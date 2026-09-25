/* 运行监控页（/monitor）
   数据源：GET /api/admin/monitor/overview（管理员）

   首屏：**页面不等采样**。服务端只回放最近一次采样（#monitor-initial，可能为空）；
   没有再自己去取 —— 采样最慢的一项按它自己的预算走，让 HTML 等它等于"打开页面
   耗时 = 最慢依赖的探测预算"。没有数据时先渲染「采集中」占位：它是无结论的，
   不会把"还没采到"画成"服务不可用"。

   刷新策略：
   - 手动「立即刷新」+ 可选定时轮询；
   - 用户主动动作（立即刷新 / 改间隔 / 重开自动刷新）带 live=1 强制现采 ——
     不复用缓存，否则按钮点了没反应、也确认不了新间隔是否生效；
   - 定时轮询与切回前台走服务端 TTL 缓存：同一瞬间的重复请求（打开页面 + 前端
     紧接着取一次、多个标签页）合并成一次探测，不为看板把依赖多探几遍；
   - 页面不可见（document.hidden）时**暂停**轮询 —— 用户切到别的标签页后
     还在后台反复探测外部服务，只会白白给 MySQL/ES/Milvus 加压；
   - 连续失败 3 次自动停表，避免对已经挂掉的服务持续打点刷屏。

   时间口径：快照自带 checkedAt（采样时刻），另有 sampledAgoSec（服务端算的
   "离现在多久"）—— 复用了缓存时必须说出来，否则用户会把上一轮的结果当成此刻。
*/
(function () {
  'use strict';

  var page = document.getElementById('monitor-page');
  if (!page) return;

  function qs(sel) { return document.querySelector(sel); }
  function qsa(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  var S = new window.State('monitor');

  // 与模板下拉项保持一致：localStorage 里可能是旧版本/被改过的值，必须回到白名单，
  // 否则会出现「下拉框显示 10 秒、实际每 7 秒打一次」这种对不上的情况。
  var INTERVAL_OPTIONS = [5, 10, 30, 60];
  var DEFAULT_INTERVAL_MS = 10000;
  var PREF_INTERVAL = 'iqa_monitor_refresh_ms';
  var PREF_AUTO = 'iqa_monitor_auto';

  // 三个 tab：基础服务（依赖健康） / 任务状态（就绪度与队列） / 指标图表
  var TAB_LIST = ['service', 'task', 'metrics'];
  var PREF_TAB = 'iqa_monitor_tab';

  var state = {
    snapshot: null,
    timer: null,
    intervalMs: DEFAULT_INTERVAL_MS,
    auto: true,
    busy: false,
    failStreak: 0,
    tab: TAB_LIST[0],
  };

  function loadPrefs() {
    var ms = Number(S.restore(PREF_INTERVAL, DEFAULT_INTERVAL_MS));
    state.intervalMs = (INTERVAL_OPTIONS.indexOf(ms / 1000) >= 0)
      ? ms : DEFAULT_INTERVAL_MS;
    state.auto = S.restore(PREF_AUTO, true) !== false;
    var tab = S.restore(PREF_TAB, TAB_LIST[0]);
    state.tab = TAB_LIST.indexOf(tab) >= 0 ? tab : TAB_LIST[0];
  }

  /* ── 小工具 ── */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function badge(text, cls) {
    var b = el('span', 'badge ' + (cls || 'badge-default'), text);
    return b;
  }

  function fmtLatency(v) {
    if (v === null || v === undefined) return '—';
    var n = Number(v);
    if (isNaN(n)) return '—';
    if (n < 1) return '<1 ms';
    if (n < 1000) return Math.round(n) + ' ms';
    return (n / 1000).toFixed(2) + ' s';
  }

  function fmtUptime(sec) {
    var s = Math.max(0, Math.floor(Number(sec) || 0));
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600);
    var m = Math.floor(s % 3600 / 60);
    if (d) return d + ' 天 ' + h + ' 小时';
    if (h) return h + ' 小时 ' + m + ' 分';
    if (m) return m + ' 分 ' + (s % 60) + ' 秒';
    return s + ' 秒';
  }

  function fmtClock(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    function p(n) { return ('0' + n).slice(-2); }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* "多久以前采的"：小于 5 秒不显示 —— 刚采完就标一句"3 秒前"只是噪声，
     用户会以为每次刷新都拿到旧数据。5 秒起才说明"这份不是此刻的"。 */
  function fmtAgo(sec) {
    var s = Math.round(Number(sec) || 0);
    if (!isFinite(s) || s < 5) return '';
    if (s < 60) return s + ' 秒前';
    if (s < 3600) return Math.round(s / 60) + ' 分钟前';
    return Math.round(s / 3600) + ' 小时前';
  }

  function fmtLimit(v) {
    return (v === null || v === undefined) ? '不限' : String(v);
  }

  /* 单个服务的状态口径（与后端 enabled / probe / online / timedOut / degraded 一一对应） */
  function statusOf(s) {
    if (!s.enabled) return { cls: 'dot-unknown', text: '未启用' };
    if (s.probe === 'static') return { cls: 'dot-unknown', text: '无探针' };
    // 本页显示预算内没等到回应：此刻确实用不了，但**不等于对端失联** ——
    // 探测还在后台按该服务自己的预算继续跑。报"超时"而不是"不可用"，因为
    // 两者要用户做的事不同：一个可以等结论落地，另一个才需要去查那个服务。
    if (s.timedOut) return { cls: 'dot-warn', text: '探测超时' };
    if (!s.online) return { cls: 'dot-error', text: '不可用' };
    if (s.degraded) return { cls: 'dot-warn', text: '降级运行' };
    if (s.localImpl) return { cls: 'dot-warn', text: '本地实现' };
    return { cls: 'dot-ok', text: '在线' };
  }

  /* ── 行内说明（ⓘ）──
     首屏方块与卡片标题只保留结论，口径说明收进气泡：点击 ⓘ 展开，
     鼠标移出（或点空白 / Esc）即收起，不长期占版面。 */
  var INFO_PAD = 8;

  function closeInfoTips() {
    qsa('.info-wrap.open').forEach(function (n) { n.classList.remove('open'); });
  }

  /* 贴边校正：靠右的方块若居中展开会顶出视口，改成向左对齐 */
  function placeInfo(wrap) {
    var pop = wrap.querySelector('.info-pop');
    if (!pop) return;
    wrap.classList.remove('pop-right', 'pop-left');
    var r = pop.getBoundingClientRect();
    if (r.right > window.innerWidth - INFO_PAD) wrap.classList.add('pop-right');
    else if (r.left < INFO_PAD) wrap.classList.add('pop-left');
  }

  function infoTip(text, cls, hover) {
    if (!text) return null;
    var wrap = el('span', 'info-wrap' + (cls ? ' ' + cls : ''));
    var dot = el('span', 'info-dot', 'i');
    dot.setAttribute('role', 'button');
    dot.setAttribute('tabindex', '0');
    dot.setAttribute('aria-label', '说明');
    // 悬停即开的那种不挂 title：原生 tooltip 会晚一秒再冒出来，与气泡叠在一起
    // 同一个位置说同一句话。键盘仍然按 Enter / 空格打开（下面照旧接了 keydown）。
    if (!hover) dot.title = '点击查看说明';
    wrap.appendChild(dot);
    wrap.appendChild(el('span', 'info-pop', text));

    function toggle(e) {
      if (e) e.stopPropagation();
      var on = !wrap.classList.contains('open');
      closeInfoTips();
      if (!on) return;
      wrap.classList.add('open');
      placeInfo(wrap);
    }
    dot.addEventListener('click', toggle);
    dot.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
    if (hover) {
      // 注册名列的 ⓘ：鼠标晃到就展开，不必先点一次 —— 这一列全是"这个短词到底是
      // 谁"的口径（模型路由、配置端点、配置名与实际运行的落差），扫一眼就想看。
      // 仍然复用 .open + placeInfo：气泡要先 display 出来才量得到位置，贴边校正
      // 才算得准；同时只开一个（closeInfoTips）与点击行为一致。
      wrap.addEventListener('mouseenter', function () {
        closeInfoTips();
        wrap.classList.add('open');
        placeInfo(wrap);
      });
    }
    wrap.addEventListener('mouseleave', function () {
      wrap.classList.remove('open', 'pop-right', 'pop-left');
    });
    return wrap;
  }

  /* 点空白处 / Esc 收起所有说明气泡 */
  function bindInfoDismiss() {
    document.addEventListener('click', function (e) {
      if (!e.target || !e.target.closest || !e.target.closest('.info-wrap')) {
        closeInfoTips();
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeInfoTips();
    });
  }

  /* ── 总览卡片 ──
     口径说明统一走 ⓘ 气泡：方块里只留「指标名 + 数值」，同一信息不重复两处。 */
  function statCard(opts) {
    var c = el('div', 'stat-card');
    var lab = el('div', 'stat-card-label');
    lab.appendChild(document.createTextNode(opts.label));
    var info = infoTip(opts.info);
    if (info) lab.appendChild(info);
    c.appendChild(lab);
    c.appendChild(el('div', 'stat-card-value' + (opts.tone ? ' ' + opts.tone : ''), opts.value));
    return c;
  }

  function renderSummary(data) {
    var frag = document.createDocumentFragment();
    var s = data.summary || {};
    var app = data.app || {};

    // 分母只用「已启用」的服务：把主动关掉的检索路算进分母，
    // 总览第一眼就是大面积红黄，真正的故障反而被淹掉。
    var denominator = s.enabledTotal || 0;
    var tone = 'ok';
    if (s.coreTotal && s.coreOnline < s.coreTotal) tone = 'error';
    else if (s.offline) tone = 'warn';
    frag.appendChild(statCard({
      label: '支撑服务',
      value: (s.online || 0) + ' / ' + denominator,
      tone: tone,
      info: '未启用 ' + (s.disabled || 0) + ' 项 · 真实外部 ' + (s.realOnline || 0) +
        ' · 本地实现 ' + (s.localImpl || 0) + ' · 无探针 ' + (s.static || 0),
    }));

    frag.appendChild(statCard({
      label: '核心依赖',
      value: (s.coreOnline || 0) + ' / ' + (s.coreTotal || 0),
      tone: s.coreOnline === s.coreTotal ? 'ok' : 'error',
      info: app.noconnection ? '演示模式（不连外部服务）' : '缺失时自动降级为本地实现',
    }));

    var lost = s.lostPaths || 0;
    frag.appendChild(statCard({
      label: '检索路',
      value: (s.livePaths || 0) + ' / ' + (s.enabledPaths || 0),
      tone: lost ? 'warn' : 'ok',
      info: lost ? '已启用但不可用 ' + lost + ' 条' : '全部就绪',
    }));

    var ing = (data.readiness || {}).ingest || {};
    frag.appendChild(statCard({
      label: '入库队列',
      value: String(ing.queuedTasks || 0) + ' / ' + fmtLimit(ing.queueDepthLimit),
      tone: ing.queueFull ? 'error' : 'ok',
      info: '并发 ' + (ing.concurrency || 0) + (ing.queueFull ? ' · 队列已满' : ''),
    }));

    // 降级记录的存放位置写在 ⓘ 里就够了：方块本身与其它五个保持一致，
    // 不做 hover 变色 / 点击跳转那套——六格里只有一格能点，只会让人猜还有没有别的隐藏操作。
    var degCount = Object.keys(data.degraded || {}).length;
    frag.appendChild(statCard({
      label: '降级记录',
      value: String(degCount),
      tone: degCount ? 'warn' : 'ok',
      info: degCount ? '见「基础服务」标签页底部' : '无',
    }));

    frag.appendChild(statCard({
      label: '运行时长',
      value: fmtUptime(app.uptimeSeconds),
      tone: 'ok',
      info: '启动于 ' + fmtClock(app.startedAt),
    }));

    return frag;
  }

  /* ── 支撑服务 ──
     每行三列：组件名（必要时带「降级」徽章） | 注册名 | 耗时。
     注册名一列放"跑的是谁"：LLM / 向量模型显示模型名，其余显示注册名（降级
     后即 mock），端点与模型路由收进这一列的 ⓘ。 */
  function registrationTip(s) {
    // 第二列的 ⓘ：这一列只有一两个词，悬停即见完整口径 —— 端点在哪、
    // 模型怎么路由、配置的注册名与当前实际运行的为何对不上。
    // 无话可说时返回空串，连 ⓘ 一起省掉（空 ⓘ 只会让人白悬停一次）。
    var parts = [];
    if (s.modelTitle) parts.push(s.modelTitle);
    // "配置的注册名 ≠ 运行名"：模型类各行（llm / 多模态 / 向量模型）由 modelTitle
    // 讲（它说的是模型，比协议名更贴用户要找的答案，别把 openai_compatible 又搬
    // 回来）；其余组件在这里补 —— 向量库降级成 memory 时，用户要确认的正是"我配的
    // milvus 还在不在配置里"，这一句是唯一能回答它的地方。
    var modelRows = ['llm', 'vlm', 'embedding'];
    if (modelRows.indexOf(s.key) < 0 && s.configuredAdapter
        && s.configuredAdapter !== s.adapter) {
      parts.push('配置注册名为 ' + s.configuredAdapter
        + '，当前实际运行 ' + s.adapter);
    }
    if (s.endpoint && s.endpoint !== '—') {
      // 端点后端一律给"配置里写了什么"：降级成替身 / 未启用 / 演示模式跳过时它
      // 仍然报出来，但必须写明"当前未使用"—— 否则这个地址会被读成"已连上这里"。
      parts.push((s.endpointInUse === false ? '配置端点（当前未使用）：'
        : '端点：') + s.endpoint);
    }
    return parts.join('；');
  }

  // demo：--noconnection 演示模式。它决定"没连上"该不该算问题 —— 演示模式下
  // 外部依赖是**按设计**不连的，刷成黄点等于把演示模式的正常状态报成故障。
  function serviceRow(s, demo) {
    var st = statusOf(s);
    var row = el('div', 'monitor-svc');

    // 说明文字一律收进 ⓘ（以前是常驻的黄字：只要有两三条不健康，版面就被
    // "为什么"占满，反而看不出"到底哪几条不健康"）。四种情况必须分清，
    // 否则用户会觉得监控页和配置页互相打脸（TS-015 口径一致）：
    // ① 静态实现：没有独立探针；
    // ② 本页显示预算内没等到回应：不等于对端失联，探测还在后台跑（见下）；
    // ③ 真的连不上：把降级原因说清楚，并交代后台正在自动重连（见下）；
    // ④ 探测已通过但容器还挂着降级：自愈循环重装该段之前的过渡态。
    //
    // 黄 ⓘ 只留给"需要你处理"的那几种：探测超时、该启用却连不上、探测通过但容器
    // 仍记着降级。用户主动关掉的段、演示模式下按设计跳过的段都只是既定状态，刷成
    // 黄色会让真正的故障淹在黄点里（以前黄字常驻时没人分得清，现在一眼可辨）。
    var note = '', warn = false;
    if (s.probe === 'static') {
      // 进程内实现（同义词表 / 认证 / mock）：没有可探测的对端，不是故障
      note = s.message || '进程内实现，无独立健康探针';
    } else if (s.timedOut) {
      warn = !!s.enabled && !demo;
      // 只报本行的探测文案，**不叠 degradedReason**：那是容器里启动自检/上一次
      // 的结论，挂在"本轮没等到"上就是张冠李戴（用户会以为容器又记了一笔）。
      note = s.message || '未在显示预算内返回';
    } else if (!s.online) {
      warn = !!s.enabled && !demo;
      note = s.degradedReason || s.message || '';
      // 失联的段后台一直在重试。不写出来的话，用户看到状态长时间不变，只会
      // 以为程序根本没在管（实际是"我在试，但对端还没起来"），于是重启或
      // 反复保存配置去瞎猜 —— 这正是本页面最该避免的一幕。
      if (s.retry) {
        note += ' · 已自动重连 ' + s.retry.attempts + ' 次，最近一次 '
          + s.retry.atText + ' 仍失败（每 ' + s.retry.intervalSec +
          ' 秒重试一次，对端恢复后自动回到可用，无需重启）';
      }
    } else if (s.degraded) {
      warn = true;
      note = (s.degradedReason || '容器仍记录着降级') +
        '（探测已通过，等待后台自愈重装该段；也可在配置页保存一次立即生效）';
    }

    var main = el('div', 'monitor-svc-main');
    var dot = el('span', 'dot ' + st.cls);
    dot.title = st.text;
    main.appendChild(dot);
    var name = el('span', 'monitor-svc-name', s.label || s.key);
    name.title = (s.label || s.key) + ' — ' + st.text;
    main.appendChild(name);
    // ⓘ 必须挂在 name **外面**：.monitor-svc-name 是 overflow:hidden 的单行
    // 省略号盒子（防长名字撑破列），气泡（position:absolute）挂进去就会被它连
    // 内容一起裁掉 —— 点开是空的，而 DOM 里文字俱全，改代码时根本看不出来。
    // main 只做 flex 布局、不裁剪，气泡放这儿才是"点得开也看得见"。
    var tip = infoTip(note, warn ? 'warn' : '');
    if (tip) {
      if (s.degraded) {
        // 有「降级」字样时把原因挂在这枚徽章上 —— 用户点它问的正是"为什么降级"；
        // 挂在旁边的组件名上，等于让他再猜一次该点哪儿。
        var dg = badge('降级', 'badge-warning');
        dg.appendChild(tip);
        main.appendChild(dg);
      } else {
        // 没有「降级」字样时（如知识图谱未配置、进程内实现无探针）挨着组件名放，
        // 否则这条说明就没地方放了。
        main.appendChild(tip);
      }
    }
    row.appendChild(main);

    // 注册名列：有模型名就显示模型名（openai_compatible 只是"走哪套协议"，
    // "qwen38-27b" 才是用户要找的答案）；后端不给模型名时（降级成 mock /
    // 演示模式）回落到注册名 —— 那正是此刻唯一能说明"到底在跑什么"的信息。
    // 显示哪一个由这里判（有没有模型名），后端不再另发开关字段：
    // 同一件事有两个出处，迟早会打架。
    //
    // 呈现方式与其它行**完全一致**（同一枚 badge）：模型名不是"另一类东西"，
    // 它只是这一列在能报出模型时该报的值；另起虚线框样式会让 LLM 行看起来像
    // 少了一列。颜色照旧按"是不是本地替身"分：黄 = 本地占位，灰 = 真实实现。
    var reg = el('div', 'monitor-svc-reg');
    var who = s.model || s.adapter || '';
    if (who) {
      reg.appendChild(badge(who, s.localImpl ? 'badge-warning' : 'badge-default'));
    } else {
      // 既没有模型名也没有运行名（该启用却没连上、实例已被摘掉）：占位 '—'。
      // 空着一格会让整行看起来像没渲染完，也让人怀疑"是不是还有个注册名没显示出来"。
      reg.appendChild(el('span', 'monitor-svc-none', '—'));
    }
    // hover=true：这一列的 ⓘ 鼠标晃到就展开（要点击的只有第一列的说明）
    var rtip = infoTip(registrationTip(s), '', true);
    if (rtip) reg.appendChild(rtip);
    row.appendChild(reg);

    row.appendChild(el('div', 'monitor-svc-latency', fmtLatency(s.latencyMs)));
    return row;
  }

  function renderServices(data) {
    // 三大类（核心依赖 / 检索与存储组件 / 基础设施）纵向排列：
    // 横向并排时每列被压到 ~340px，端点与说明频繁折行，纵向单列更好读。
    var wrap = el('div', 'monitor-groups monitor-groups-vertical');
    var services = data.services || [];
    var demo = !!((data.app || {}).noconnection);
    (data.groups || []).forEach(function (g) {
      var rows = services.filter(function (s) { return s.category === g.key; });
      if (!rows.length) return;
      var enabledRows = rows.filter(function (r) { return r.enabled; });
      var online = enabledRows.filter(function (r) { return r.online; }).length;

      var card = el('div', 'card');
      var head = el('div', 'card-header');
      // 分组口径说明收进标题旁的 ⓘ：默认不占版面，需要时点开看。
      var title = el('h3');
      title.appendChild(document.createTextNode(g.label));
      var tip = infoTip(g.hint);
      if (tip) title.appendChild(tip);
      head.appendChild(title);
      head.appendChild(badge(online + ' / ' + enabledRows.length,
        enabledRows.length === 0 ? 'badge-default'
          : (online === enabledRows.length ? 'badge-success'
            : (online ? 'badge-warning' : 'badge-error'))));
      if (rows.length > enabledRows.length) {
        head.appendChild(badge('未启用 ' + (rows.length - enabledRows.length),
          'badge-default'));
      }
      card.appendChild(head);

      var body = el('div', 'card-body');
      var list = el('div', 'monitor-svc-list');
      rows.forEach(function (s) { list.appendChild(serviceRow(s, demo)); });
      body.appendChild(list);
      card.appendChild(body);
      wrap.appendChild(card);
    });
    // 降级记录描述的是「适配器被换成了本地替身」，属于依赖健康问题，
    // 跟着基础服务走；任务状态 tab 只留就绪度与队列。
    var deg = renderDegraded(data);
    if (deg) wrap.appendChild(deg);
    return wrap;
  }

  /* ── 业务就绪度 ── */
  function kvCard(title, badgeNode, items, info) {
    var card = el('div', 'card');
    var head = el('div', 'card-header');
    var h = el('h3');
    h.appendChild(document.createTextNode(title));
    var tip = infoTip(info);
    if (tip) h.appendChild(tip);
    head.appendChild(h);
    if (badgeNode) head.appendChild(badgeNode);
    card.appendChild(head);
    var body = el('div', 'card-body');

    var kvs = items.filter(function (it) { return it && it.value !== undefined; });
    if (kvs.length) {
      var grid = el('div', 'monitor-kv');
      kvs.forEach(function (it) {
        var box = el('div');
        box.appendChild(el('div', 'monitor-kv-label', it.label));
        var v = el('div', 'monitor-kv-value' + (it.mono ? ' mono' : ''), it.value);
        if (it.title) v.title = it.title;
        box.appendChild(v);
        grid.appendChild(box);
      });
      body.appendChild(grid);
    }
    card.appendChild(body);
    return { card: card, body: body };
  }

  function renderRetrieval(r) {
    var paths = r.enabled || [], live = r.live || [], lost = r.lost || [];
    var node = kvCard('检索路就绪度',
      badge(live.length + ' / ' + paths.length,
        lost.length ? 'badge-warning' : 'badge-success'),
      []);
    var body = node.body;

    var grid = el('div', 'monitor-kv');
    var box = el('div');
    box.style.gridColumn = '1 / -1';
    box.appendChild(el('div', 'monitor-kv-label', '实时可用（配置启用 ∩ 适配器在线）'));
    var wrap = el('div', 'monitor-paths');
    wrap.style.marginTop = '6px';
    if (!live.length) {
      wrap.appendChild(el('span', 'monitor-kv-value', '无可用检索路'));
    }
    live.forEach(function (p) {
      var b = badge(p.description || p.name, 'badge-primary');
      b.title = p.name;
      wrap.appendChild(b);
    });
    box.appendChild(wrap);
    grid.appendChild(box);
    body.appendChild(grid);

    if (lost.length) {
      var list = el('div', 'monitor-lost-list');
      lost.forEach(function (p) {
        // reason 只在"服务行全绿、路却是关的"时才有（见 routes._lost_path_reason）：
        // 那正是用户最看不懂的一种，必须说清为什么 —— 否则只会以为系统在无缘
        // 无故少查一路。服务行红着的不重复给原因（那一行有自己的 message）。
        var why = p.reason
          ? '：' + p.reason
          : '：已启用但后端不可用，本次问答不会走这条路';
        list.appendChild(el('div', null,
          '⚠ ' + (p.description || p.name) + '（' + p.name + '）' + why));
      });
      body.appendChild(list);
    }
    return node.card;
  }

  function renderIngest(r) {
    var ing = r.ingest || {}, wf = r.workflows || {};
    var names = wf.ingestNames || [];
    var node = kvCard('入库队列与工作流',
      badge((ing.queuedTasks || 0) + ' 排队' + (ing.queueFull ? ' · 已满' : ''),
        ing.queueFull ? 'badge-error' : 'badge-info'), [
        { label: '排队任务', value: String(ing.queuedTasks || 0) },
        { label: '队列上限', value: fmtLimit(ing.queueDepthLimit) },
        { label: '入库并发', value: String(ing.concurrency || 0) },
        { label: '重试上限', value: String(ing.maxRetries || 0) },
        { label: '入库后验证抽样', value: String(ing.verifySampleSize || 0) + ' 条' },
        { label: '入库工作流', value: names.length + ' 条', title: names.join('、') },
        { label: '查询工作流步骤', value: String((wf.querySteps || []).length) + ' 步',
          title: (wf.querySteps || []).join(' → ') },
      ]);
    if (names.length) {
      var hint = el('div', 'form-hint', '工作流：' + names.join('、'));
      hint.style.marginTop = '10px';
      node.body.appendChild(hint);
    }
    return node.card;
  }

  function renderOps(r) {
    var cc = r.consistencyCheck || {}, rr = r.rerank || {}, ob = r.observability || {};
    return kvCard('后台任务与可观测性',
      badge(cc.enabled ? '一致性巡检已开启' : '一致性巡检已关闭',
        cc.enabled ? 'badge-success' : 'badge-default'), [
        { label: '一致性巡检间隔', value: cc.enabled ? cc.intervalHours + ' 小时' : '—' },
        { label: '巡检抽样 / 告警阈值',
          value: cc.sampleDocs + ' 篇 / ' + cc.alertThreshold + ' 处' },
        { label: '重排（Rerank）',
          value: rr.enabled ? '已开启' : '未开启' },
        { label: '重排模型 / 设备',
          value: rr.enabled ? ((rr.model || '—') + ' · ' + (rr.device || '—')) : '—' },
        { label: '日志级别', value: ob.logLevel || '—' },
        { label: '指标与推送',
          value: (ob.metricsEnabled ? '指标已开启' : '指标已关闭') +
                 (ob.pushgateway ? ' · ' + ob.pushgateway : ' · 不推送') },
        { label: '链路追踪', value: ob.tracingEnabled ? '已开启' : '未开启' },
      ]).card;
  }

  // 容器 degraded 的键 → 页面显示名。不映射的键原样显示（后端新加的记录
  // 也会漏出来，不会被前端悄悄吃掉）；"vector_space" 不是某一段依赖，而是
  // 一条**前提条件**，所以必须单独起个名字，免得被读成"向量库又挂了"
  var DEGRADED_LABELS = {
    vector_space: '向量空间（库内向量与当前向量模型）',
  };

  function renderDegraded(data) {
    var entries = Object.entries(data.degraded || {});
    if (!entries.length) return null;
    // 降级记录不是"终审判决"：后台每 N 秒重连一次，对端恢复就自动清空。
    // 不写明这一点的话，用户修好 Milvus 后盯着这条记录只会以为还得重启。
    // 这句话收进标题旁的 ⓘ，正文只留记录本身。
    //
    // 但"自动清空"只对**依赖连接**类记录成立：向量空间不一致、模型未就绪这类
    // 前提条件不会因为对端恢复而消失（重连也修不好），必须写明，否则用户会一直
    // 等自动恢复。
    var info = '';
    if (data.recovery && data.recovery.enabled) {
      info = '进程每 ' + data.recovery.intervalSec +
        ' 秒自动重连一次失联的依赖，对端恢复后相应的记录会自动清空；' +
        '与依赖连接无关的记录（如「向量空间」）不会自行消失，需按其说明处置。';
    }
    var node = kvCard('降级记录',
      badge(entries.length + ' 条', 'badge-warning'), [], info);
    var ul = el('ul', 'monitor-degraded-list');
    entries.forEach(function (kv) {
      var li = el('li');
      li.appendChild(el('b', null, DEGRADED_LABELS[kv[0]] || kv[0]));
      li.appendChild(document.createTextNode('：' + kv[1]));
      ul.appendChild(li);
    });
    node.body.appendChild(ul);
    return node.card;
  }

  function renderReadiness(data) {
    var r = data.readiness || {};
    var wrap = el('div', 'monitor-groups');
    // 注意层级：检索路在 readiness.retrieval 下，而入库/后台项在 readiness 直属
    wrap.appendChild(renderRetrieval(r.retrieval || {}));
    wrap.appendChild(renderIngest(r));
    wrap.appendChild(renderOps(r));
    return wrap;
  }

  /* ── 指标图表 ──
     全部用快照内的即时数据现画（纯 DOM/CSS + 内联 SVG），不引第三方图表库：
     这一页是运维速览，没必要为三张图挂上几百 KB 的依赖。 */
  var SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }

  function chartCard(title, badgeNode, hint) {
    var card = el('div', 'card');
    var head = el('div', 'card-header');
    head.appendChild(el('h3', null, title));
    if (badgeNode) head.appendChild(badgeNode);
    card.appendChild(head);
    var body = el('div', 'card-body');
    if (hint) {
      var h = el('div', 'form-hint', hint);
      h.style.marginBottom = '12px';
      body.appendChild(h);
    }
    card.appendChild(body);
    return { card: card, body: body };
  }

  function emptyChart(text) { return el('div', 'mchart-empty', text); }

  function legendItem(tone, label, value) {
    var it = el('div', 'mchart-legend-item');
    it.appendChild(el('span', 'mchart-legend-dot ' + tone));
    it.appendChild(el('span', 'mchart-legend-label', label));
    it.appendChild(el('span', 'mchart-legend-value', value));
    return it;
  }

  /* 探测延迟条形图：只画真实探针（probe==='live'）且在线的项 ——
     本地替身与无探针根本没有可比耗时，混进来会把图拉成一条平线。 */
  function latencyChart(data) {
    var rows = (data.services || []).filter(function (s) {
      return s.enabled && s.online && s.probe === 'live' &&
        typeof s.latencyMs === 'number' && isFinite(s.latencyMs);
    }).sort(function (a, b) { return b.latencyMs - a.latencyMs; });

    var node = chartCard('服务探测延迟',
      badge(rows.length + ' 项可测', rows.length ? 'badge-info' : 'badge-default'),
      '最近一次真实网络／引擎探测的往返耗时（不含本地替身与无探针项）。');
    if (!rows.length) {
      node.body.appendChild(emptyChart('本次快照没有可用的真实探测耗时。'));
      return node.card;
    }
    var max = rows[0].latencyMs || 1;
    var list = el('div', 'mchart-bars');
    rows.forEach(function (s) {
      var row = el('div', 'mchart-bar-row');
      var name = el('div', 'mchart-bar-name', s.label || s.key);
      name.title = s.label || s.key;
      row.appendChild(name);
      var track = el('div', 'mchart-bar-track');
      var fill = el('div', 'mchart-bar-fill ' +
        (s.degraded || s.localImpl ? 'warn' : 'ok'));
      fill.style.width = Math.max(2, (s.latencyMs / max) * 100) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('div', 'mchart-bar-val', fmtLatency(s.latencyMs)));
      list.appendChild(row);
    });
    node.body.appendChild(list);
    return node.card;
  }

  /* 检索路就绪环形图：分母是「配置启用」的路，差的那些就是配了但连不上 */
  function donutChart(data) {
    var r = (data.readiness || {}).retrieval || {};
    var live = (r.live || []).length;
    var lost = (r.lost || []).length;
    var total = live + lost;
    var rate = total ? Math.round(live / total * 100) : 0;

    var node = chartCard('检索路就绪构成',
      badge(live + ' / ' + total, lost ? 'badge-warning' : 'badge-success'),
      '分母为配置中「已启用」的检索路；已启用但后端不可用的会在这里显形。');

    var row = el('div', 'mchart-donut-row');
    var wrap = el('div', 'mchart-donut-wrap');
    var R = 54, C = 66, CIRC = 2 * Math.PI * R;
    var svg = svgEl('svg', {
      viewBox: '0 0 132 132', class: 'mchart-donut',
      role: 'img', 'aria-label': '检索路就绪 ' + live + ' / ' + total,
    });
    svg.appendChild(svgEl('circle', { cx: C, cy: C, r: R, class: 'mchart-donut-seg track' }));
    var acc = 0;
    [{ v: live, cls: 'ok' }, { v: lost, cls: 'warn' }].forEach(function (seg) {
      if (!seg.v || !total) return;
      var len = seg.v / total * CIRC;
      svg.appendChild(svgEl('circle', {
        cx: C, cy: C, r: R, class: 'mchart-donut-seg ' + seg.cls,
        'stroke-dasharray': len + ' ' + (CIRC - len),
        'stroke-dashoffset': (-acc).toFixed(2),
        transform: 'rotate(-90 ' + C + ' ' + C + ')',
      }));
      acc += len;
    });
    wrap.appendChild(svg);
    var center = el('div', 'mchart-donut-center');
    center.appendChild(el('b', null, rate + '%'));
    center.appendChild(el('span', null, '就绪'));
    wrap.appendChild(center);
    row.appendChild(wrap);

    var legend = el('div', 'mchart-legend');
    legend.appendChild(legendItem('ok', '实时可用', live + ' 条'));
    legend.appendChild(legendItem('warn', '已启用但不可用', lost + ' 条'));
    row.appendChild(legend);
    node.body.appendChild(row);
    return node.card;
  }

  /* 入库队列水位：排队数 / 队列上限 */
  function queueChart(data) {
    var ing = (data.readiness || {}).ingest || {};
    var queued = ing.queuedTasks || 0;
    var limit = ing.queueDepthLimit;
    var hasLimit = typeof limit === 'number' && limit > 0;
    var pct = hasLimit ? Math.min(100, Math.round(queued / limit * 100)) : 0;

    var node = chartCard('入库队列水位',
      badge(queued + ' 排队' + (ing.queueFull ? ' · 已满' : ''),
        ing.queueFull ? 'badge-error' : 'badge-info'), '');

    var gauge = el('div', 'mchart-gauge');
    var track = el('div', 'mchart-gauge-track');
    var fill = el('div', 'mchart-gauge-fill' +
      (ing.queueFull ? ' error' : (pct >= 60 ? ' warn' : '')));
    fill.style.width = hasLimit
      ? (queued ? Math.max(2, pct) : 0) + '%'
      : (queued ? 100 : 0) + '%';
    track.appendChild(fill);
    gauge.appendChild(track);
    var meta = el('div', 'mchart-gauge-meta');
    meta.appendChild(el('span', null, '排队 ' + queued + ' / 上限 ' + fmtLimit(limit)));
    meta.appendChild(el('span', null, hasLimit ? pct + '%' : '未设上限'));
    gauge.appendChild(meta);
    node.body.appendChild(gauge);

    var kvs = el('div', 'monitor-kv');
    kvs.style.marginTop = '14px';
    [['入库并发', String(ing.concurrency || 0)],
     ['重试上限', String(ing.maxRetries || 0)],
     ['入库后验证抽样', String(ing.verifySampleSize || 0) + ' 条']
    ].forEach(function (it) {
      var box = el('div');
      box.appendChild(el('div', 'monitor-kv-label', it[0]));
      box.appendChild(el('div', 'monitor-kv-value', it[1]));
      kvs.appendChild(box);
    });
    node.body.appendChild(kvs);
    return node.card;
  }

  function renderMetrics(data) {
    var wrap = el('div', 'monitor-groups monitor-groups-vertical');
    wrap.appendChild(latencyChart(data));
    wrap.appendChild(donutChart(data));
    wrap.appendChild(queueChart(data));
    var ob = (data.readiness || {}).observability || {};
    var note = el('div', 'form-hint');
    note.textContent = '以上为单次快照内的即时指标。' + (ob.metricsEnabled
      ? '时序指标已开启' + (ob.pushgateway ? '（推送 ' + ob.pushgateway + '）' : '') +
        '，可由 Prometheus 采集后在此扩展趋势图。'
      : '如需时序趋势，可在「任务状态」标签页开启指标与推送。');
    wrap.appendChild(note);
    return wrap;
  }

  /* ── Tab 切换 ── */
  function switchTab(tab, opts) {
    opts = opts || {};
    if (TAB_LIST.indexOf(tab) < 0) tab = TAB_LIST[0];
    state.tab = tab;
    qsa('[data-mtab]').forEach(function (b) {
      var on = b.getAttribute('data-mtab') === tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    qsa('[data-mtab-panel]').forEach(function (p) {
      p.classList.toggle('hidden', p.getAttribute('data-mtab-panel') !== tab);
    });
    if (opts.persist !== false) S.persist(PREF_TAB, tab);
  }

  function bindTabs() {
    var bar = qs('#monitor-tabs');
    if (bar) {
      bar.addEventListener('click', function (e) {
        var btn = e.target && e.target.closest
          ? e.target.closest('[data-mtab]') : null;
        if (btn) switchTab(btn.getAttribute('data-mtab'));
      });
    }
    switchTab(state.tab, { persist: false });
  }

  /* ── 顶部提示条 ──
     快照正常时不再往方块上方贴摘要：「不可用服务 / 降级记录」的口径在下方六个
     方块与「基础服务」tab 里都有，重复一行只会把首屏挤乱。提示条只留给刷新失败。 */
  function renderBanner() {
    var b = qs('#monitor-banner');
    if (!b) return;
    b.className = 'monitor-banner hidden';
    b.textContent = '';
  }

  function renderStamp(data) {
    var st = qs('#monitor-stamp');
    if (!st) return;
    // 只报「最后更新」时刻：刷新间隔在工具栏的下拉里明摆着，重复一遍没意义。
    // 但「停了」必须说 —— 否则用户会以为页面还在更新，实际它已经不探了。
    var head = '最后更新 ' + (fmtClock((data || {}).checkedAt) || '—');
    // 命中了服务端缓存也要说：那一刻显示的是**上一轮**的采样结果，不标注的话，
    // "最后更新"会被读成"此刻的状态"，而这正是监控页最不能有的误导。
    var ago = fmtAgo((data || {}).sampledAgoSec);
    if (ago) head += ' · 采样于 ' + ago;
    if (document.hidden) {
      st.textContent = head + ' · 页面在后台，已暂停刷新';
    } else if (!state.auto) {
      st.textContent = head + ' · 已暂停自动刷新';
    } else {
      st.textContent = head;
    }
  }

  /* ── 首屏占位 ──
     页面不再等采样，所以在数据到达前必须有一块**无结论**的占位：只说"在采"，
     不画任何状态色。若改成先渲染一份空快照（全灰/全红），用户第一眼看到的就是
     "全线故障"——监控页最贵的一次误报，恰恰发生在它最可能被打开的那一刻。 */
  function skeletonCard(text) {
    var card = el('div', 'card');
    var body = el('div', 'card-body monitor-skeleton');
    body.appendChild(el('span', 'monitor-spinner'));
    body.appendChild(el('span', null, text));
    card.appendChild(body);
    return card;
  }

  function renderSkeleton() {
    var st = qs('#monitor-stamp');
    if (st) st.textContent = '正在采集…（逐项探测依赖，结果到齐后自动填入）';
    [['monitor_summary', '总览指标采集中'],
     ['monitor_services', '支撑服务探测中'],
     ['monitor_readiness', '就绪度采集中'],
     ['monitor_metrics', '指标采集中']
    ].forEach(function (it) {
      var root = document.querySelector('[data-piece="' + it[0] + '"]');
      if (!root) return;
      while (root.firstChild) root.removeChild(root.firstChild);
      root.appendChild(skeletonCard(it[1]));
    });
  }

  function applySnapshot(data) {
    state.snapshot = data;
    window.Partials.renderPiece('monitor_summary', data);
    window.Partials.renderPiece('monitor_services', data);
    window.Partials.renderPiece('monitor_readiness', data);
    window.Partials.renderPiece('monitor_metrics', data);
    renderBanner();
    renderStamp(data);
  }

  /* ── 轮询 ── */
  function clearTimer() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  function schedule() {
    clearTimer();
    if (!state.auto || document.hidden) return;
    state.timer = setInterval(refresh, state.intervalMs);
  }

  async function refresh(opts) {
    opts = opts || {};
    if (state.busy) return;
    state.busy = true;
    var st = qs('#monitor-stamp');
    if (st) st.classList.add('busy');
    try {
      // live=1：用户主动要一次现采（点了按钮却拿到 15 秒内的缓存，按钮就成了
      // "点了没反应"）。默认不带，让同一瞬间的重复请求在服务端合并成一次探测。
      var data = await window.API.getData(
        '/api/admin/monitor/overview' + (opts.live ? '?live=1' : ''),
        { timeout: 25000 });
      state.failStreak = 0;
      applySnapshot(data);
    } catch (e) {
      state.failStreak++;
      var b = qs('#monitor-banner');
      if (b) {
        b.className = 'monitor-banner error';
        b.textContent = '刷新失败：' + (e.message || String(e)) +
          '（第 ' + state.failStreak + ' 次，页面显示的是上一次快照）';
      }
      if (state.failStreak === 1 && window.Partials && window.Partials.showToast) {
        window.Partials.showToast('监控快照刷新失败：' + (e.message || e), 'error');
      }
      if (state.failStreak >= 3) {
        // 连续失败：停止自动轮询，改由用户手动刷新，避免无效打点
        state.auto = false;
        var cb = qs('#monitor-auto');
        if (cb) cb.checked = false;
        clearTimer();
        renderStamp(state.snapshot || {});
      }
    } finally {
      state.busy = false;
      if (st) st.classList.remove('busy');
    }
  }

  /* ── 初始化 ── */
  /* 服务端回放的最近一次采样（可能为 null —— 那说明没有够新的可复用结果，
     页面不等它，交给下面的 refresh()）。解析失败同样按"没有"处理：
     占位 + 自己取一次，总好过空白页。 */
  function bootstrapSnapshot() {
    var node = document.getElementById('monitor-initial');
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || '{}');
    } catch (e) {
      console.error('[monitor] 首屏快照解析失败', e);
      return null;
    }
  }

  function bindToolbar() {
    var btn = qs('#monitor-refresh');
    // 用户主动要一次真实探测，不走缓存
    if (btn) btn.addEventListener('click', function () { refresh({ live: true }); });

    var auto = qs('#monitor-auto');
    if (auto) {
      auto.checked = state.auto;
      auto.addEventListener('change', function () {
        state.auto = !!auto.checked;
        state.failStreak = 0;
        S.persist(PREF_AUTO, state.auto);
        if (state.auto) { refresh({ live: true }); }
        else if (state.snapshot) { renderStamp(state.snapshot); }
        schedule();
      });
    }

    var sel = qs('#monitor-interval');
    if (sel) {
      sel.value = String(state.intervalMs / 1000);
      sel.addEventListener('change', function () {
        var v = parseInt(sel.value, 10);
        if (typeof v === 'number' && !isNaN(v) && v > 0) {
          state.intervalMs = v * 1000;
          S.persist(PREF_INTERVAL, state.intervalMs);
        }
        // 立即刷新一次：否则用户改完间隔后要干等一个周期才看得到反馈，
        // 无法确认新频率是否生效（顺带把时间戳文案一起更新）。现采 —— 若命中
        // 缓存，采样时刻不变，用户照样确认不了"刚才那次到底跑没跑"。
        if (state.auto) { refresh({ live: true }); }
        else if (state.snapshot) { renderStamp(state.snapshot); }
        schedule();
      });
    }

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        clearTimer();
        renderStamp(state.snapshot || {});
        return;
      }
      renderStamp(state.snapshot || {});
      // 切回前台：隐藏期间定时器是停的，回来先补一次。允许命中缓存（通常也命中
      // 不了：隐藏久了早就过了 TTL），命中的话时间戳会写明"采样于 N 分钟前"。
      if (state.auto) { refresh(); schedule(); }
    });

    window.addEventListener('beforeunload', clearTimer);
  }

  window.Partials.registerPieceRenderer('monitor_summary', renderSummary);
  window.Partials.registerPieceRenderer('monitor_services', renderServices);
  window.Partials.registerPieceRenderer('monitor_readiness', renderReadiness);
  window.Partials.registerPieceRenderer('monitor_metrics', renderMetrics);

  loadPrefs();
  bindTabs();
  bindToolbar();
  bindInfoDismiss();

  var initial = bootstrapSnapshot();
  if (initial) {
    applySnapshot(initial);
  } else {
    // 没有可回放的采样：先摆无结论的占位，立刻自己去取。这一次请求通常会命中
    // 服务端正在跑的那一轮探测（单飞），而不是再探一遍。
    renderSkeleton();
    refresh();
  }
  schedule();
})();
