/* 运行监控页（/monitor）
   数据源：GET /api/admin/monitor/overview（管理员）
   首屏：服务端随模板下发的快照（#monitor-initial），免一次往返闪烁

   刷新策略：
   - 手动「立即刷新」+ 可选定时轮询；
   - 页面不可见（document.hidden）时**暂停**轮询 —— 用户切到别的标签页后
     还在后台反复探测外部服务，只会白白给 MySQL/ES/Milvus 加压；
   - 连续失败 3 次自动停表，避免对已经挂掉的服务持续打点刷屏。
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

  function fmtLimit(v) {
    return (v === null || v === undefined) ? '不限' : String(v);
  }

  /* 单个服务的状态口径（与后端 enabled / probe / online / degraded 一一对应） */
  function statusOf(s) {
    if (!s.enabled) return { cls: 'dot-unknown', text: '未启用' };
    if (s.probe === 'static') return { cls: 'dot-unknown', text: '无探针' };
    if (!s.online) return { cls: 'dot-error', text: '不可用' };
    if (s.degraded) return { cls: 'dot-warn', text: '降级运行' };
    if (s.localImpl) return { cls: 'dot-warn', text: '本地实现' };
    return { cls: 'dot-ok', text: '在线' };
  }

  /* ── 总览卡片 ── */
  function statCard(label, value, sub, tone) {
    var c = el('div', 'stat-card');
    c.appendChild(el('div', 'stat-card-label', label));
    var v = el('div', 'stat-card-value' + (tone ? ' ' + tone : ''), value);
    c.appendChild(v);
    if (sub) c.appendChild(el('div', 'stat-card-sub', sub));
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
    frag.appendChild(statCard(
      '支撑服务在线', (s.online || 0) + ' / ' + denominator,
      '未启用 ' + (s.disabled || 0) + ' 项 · 真实外部 ' + (s.realOnline || 0) +
      ' · 本地实现 ' + (s.localImpl || 0) + ' · 无探针 ' + (s.static || 0),
      tone));

    frag.appendChild(statCard(
      '核心依赖', (s.coreOnline || 0) + ' / ' + (s.coreTotal || 0),
      (app.noconnection ? '演示模式（不连外部服务）' : '缺失时自动降级为本地实现'),
      s.coreOnline === s.coreTotal ? 'ok' : 'error'));

    var lost = s.lostPaths || 0;
    frag.appendChild(statCard(
      '检索路', (s.livePaths || 0) + ' / ' + (s.enabledPaths || 0),
      lost ? '已启用但不可用 ' + lost + ' 条' : '全部就绪',
      lost ? 'warn' : 'ok'));

    var ing = (data.readiness || {}).ingest || {};
    frag.appendChild(statCard(
      '入库队列',
      String(ing.queuedTasks || 0) + ' / ' + fmtLimit(ing.queueDepthLimit),
      '并发 ' + (ing.concurrency || 0) + (ing.queueFull ? ' · 队列已满' : ''),
      ing.queueFull ? 'error' : 'ok'));

    // 降级记录列表在「基础服务」tab 底部：给它一个可点击的入口，
    // 免得用户在当前 tab 里上下找不到「下方」到底是哪儿。
    var degCount = Object.keys(data.degraded || {}).length;
    var degCard = statCard(
      '降级记录', String(degCount),
      degCount ? '见「基础服务」标签页底部' : '无',
      degCount ? 'warn' : 'ok');
    if (degCount) {
      degCard.classList.add('stat-card-link');
      degCard.title = '点击查看「基础服务」标签页的降级记录';
      degCard.addEventListener('click', function () { switchTab('service'); });
    }
    frag.appendChild(degCard);

    frag.appendChild(statCard(
      '进程运行时长', fmtUptime(app.uptimeSeconds),
      '启动于 ' + fmtClock(app.startedAt), 'ok'));

    return frag;
  }

  /* ── 支撑服务 ── */
  function serviceRow(s) {
    var st = statusOf(s);
    var row = el('div', 'monitor-svc');

    var main = el('div', 'monitor-svc-main');
    var dot = el('span', 'dot ' + st.cls);
    dot.title = st.text;
    main.appendChild(dot);
    var name = el('span', 'monitor-svc-name', s.label || s.key);
    name.title = (s.label || s.key) + ' — ' + st.text;
    main.appendChild(name);
    if (s.adapter) {
      var ab = badge(s.adapter, s.localImpl ? 'badge-warning' : 'badge-default');
      if (s.configuredAdapter && s.configuredAdapter !== s.adapter) {
        ab.title = '配置为 ' + s.configuredAdapter + '，当前实际运行 ' + s.adapter;
      }
      main.appendChild(ab);
    }
    if (s.degraded) main.appendChild(badge('降级', 'badge-warning'));
    row.appendChild(main);

    var ep = el('div', 'monitor-svc-endpoint', s.endpoint || '—');
    ep.title = s.endpoint || '';
    row.appendChild(ep);

    row.appendChild(el('div', 'monitor-svc-latency', fmtLatency(s.latencyMs)));

    // 「探测通过 + 仍有降级记录」是真实存在的组合：容器里的降级要等该段配置
    // 重新保存才会清除。这里把两句话都说清楚，否则用户会觉得监控页和配置页
    // 互相打脸（TS-015 口径一致）。
    var note = '', noteCls = 'monitor-svc-note';
    if (s.probe === 'static') {
      note = s.message || '进程内实现，无独立健康探针';
      noteCls += ' info';
    } else if (!s.online) {
      note = s.degradedReason || s.message || '';
    } else if (s.degraded) {
      note = (s.degradedReason || '容器仍记录着降级') +
        (s.localImpl ? '' : '（当前探测已通过，保存该段配置后降级记录会清除）');
    }
    if (note) {
      row.appendChild(el('div', noteCls, note));
    }
    return row;
  }

  function renderServices(data) {
    // 三大类（核心依赖 / 检索与存储后端 / 基础设施）纵向排列：
    // 横向并排时每列被压到 ~340px，端点与说明频繁折行，纵向单列更好读。
    var wrap = el('div', 'monitor-groups monitor-groups-vertical');
    var services = data.services || [];
    (data.groups || []).forEach(function (g) {
      var rows = services.filter(function (s) { return s.category === g.key; });
      if (!rows.length) return;
      var enabledRows = rows.filter(function (r) { return r.enabled; });
      var online = enabledRows.filter(function (r) { return r.online; }).length;

      var card = el('div', 'card');
      var head = el('div', 'card-header');
      head.appendChild(el('h3', null, g.label));
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
      var hint = el('div', 'form-hint', g.hint || '');
      hint.style.marginBottom = '8px';
      body.appendChild(hint);

      var list = el('div', 'monitor-svc-list');
      rows.forEach(function (s) { list.appendChild(serviceRow(s)); });
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
  function kvCard(title, badgeNode, items) {
    var card = el('div', 'card');
    var head = el('div', 'card-header');
    head.appendChild(el('h3', null, title));
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
        list.appendChild(el('div', null,
          '⚠ ' + (p.description || p.name) + '（' + p.name + '）：已启用但后端不可用，本次问答不会走这条路'));
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

  function renderDegraded(data) {
    var entries = Object.entries(data.degraded || {});
    if (!entries.length) return null;
    var node = kvCard('降级记录',
      badge(entries.length + ' 条', 'badge-warning'), []);
    var ul = el('ul', 'monitor-degraded-list');
    entries.forEach(function (kv) {
      var li = el('li');
      li.appendChild(el('b', null, kv[0]));
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

  /* ── 顶部提示条 ── */
  function renderBanner(data) {
    var b = qs('#monitor-banner');
    if (!b) return;
    var offline = (data.services || []).filter(function (s) {
      return s.enabled && !s.online;
    });
    var degCount = Object.keys(data.degraded || {}).length;
    if (!offline.length && !degCount) {
      b.className = 'monitor-banner hidden';
      b.textContent = '';
      return;
    }
    var parts = [];
    if (offline.length) {
      parts.push('不可用服务：' + offline.map(function (s) { return s.label; }).join('、'));
    }
    if (degCount) parts.push('降级记录 ' + degCount + ' 条（见「基础服务」标签页底部）');
    b.className = 'monitor-banner ' + (offline.length ? 'error' : 'warning');
    b.textContent = parts.join('；');
  }

  function renderStamp(data) {
    var st = qs('#monitor-stamp');
    if (!st) return;
    var head = '最后更新 ' + (fmtClock((data || {}).checkedAt) || '—');
    if (document.hidden) {
      // 后台标签页已停表，这里如实说明，否则「每 N 秒刷新」会让人以为
      // 页面在后台也在持续探测（那正是我们要避免的）。
      st.textContent = head + ' · 页面在后台，已暂停刷新';
    } else if (!state.auto) {
      st.textContent = head + ' · 已暂停自动刷新';
    } else {
      st.textContent = head + ' · 每 ' + (state.intervalMs / 1000) + ' 秒刷新';
    }
  }

  function applySnapshot(data) {
    state.snapshot = data;
    window.Partials.renderPiece('monitor_summary', data);
    window.Partials.renderPiece('monitor_services', data);
    window.Partials.renderPiece('monitor_readiness', data);
    window.Partials.renderPiece('monitor_metrics', data);
    renderBanner(data);
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

  async function refresh() {
    if (state.busy) return;
    state.busy = true;
    var st = qs('#monitor-stamp');
    if (st) st.classList.add('busy');
    try {
      var data = await window.API.getData('/api/admin/monitor/overview',
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
    if (btn) btn.addEventListener('click', function () { refresh(); });

    var auto = qs('#monitor-auto');
    if (auto) {
      auto.checked = state.auto;
      auto.addEventListener('change', function () {
        state.auto = !!auto.checked;
        state.failStreak = 0;
        S.persist(PREF_AUTO, state.auto);
        if (state.auto) { refresh(); }
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
        // 无法确认新频率是否生效（顺带把时间戳文案一起更新）。
        if (state.auto) { refresh(); } else if (state.snapshot) { renderStamp(state.snapshot); }
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

  var initial = bootstrapSnapshot();
  if (initial) {
    applySnapshot(initial);
  } else {
    refresh();
  }
  schedule();
})();
