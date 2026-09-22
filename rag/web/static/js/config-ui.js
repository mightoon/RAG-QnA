/* 配置页渲染：模型/服务/高级/权限/系统 五个面板的 DOM 构建器 */
(function () {
  'use strict';
  const esc = s => window.utils.escapeHtml(s == null ? '' : s);
  const showToast = (msg, type, duration) => {
    if (window.Partials && window.Partials.showToast) {
      window.Partials.showToast(msg, type, duration);   // duration=0 → 常驻，点 ✕ 关闭
    } else console.log('[toast]', type, msg);
  };

  /* 模型 tab 下的报错统一 3 秒自动关闭（既不常驻、也不给 ✕）：测试没过 / 拉不到
     模型 / 入库失败这类提示说的都是"这一次没成功"，看过了就不该再占着屏幕。
     其它 tab 保持原来的常驻行为 —— 那边的失败提示往往要用户照着它回填内容。 */
  const MODEL_ERR_TTL = 3000;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // 容器 degraded 的键 → 页面显示名（同时用于保存后的 toast 与「降级组件」卡片）。
  // 未映射的键原样显示：后端新加的记录也会漏出来，不会被前端悄悄吃掉。
  // 「向量空间」不是某一段依赖，而是一条前提条件，单独给名字免得被读成
  // "向量库又挂了"（处置方式也完全不同：换库重连没用，要么重跑入库、要么换集合前缀）
  const DEGRADED_LABELS = {
    vector_space: '向量空间（库内向量与当前向量模型）',
  };
  const degradedName = k => DEGRADED_LABELS[k] || k;

  function kvRow(labelText, input, required, optional) {
    const row = el('div', 'perm-row');
    const lab = el('label', 'form-label');
    lab.style.minWidth = '130px';
    lab.style.margin = '0';
    lab.textContent = labelText;
    if (required) {
      const star = el('span', 'required', ' *');
      lab.appendChild(star);
    }
    row.appendChild(lab);
    row.appendChild(input);
    if (optional) {
      const tag = el('span', 'optional-tag', 'optional');
      tag.title = '可留空：留空即使用服务端的免密/默认配置';
      row.appendChild(tag);
    }
    return row;
  }

  /* 行下方的补充说明（另起一行）：.perm-row 是横向 flex，塞进去会挤在输入框右侧 */
  function hintLine(text) {
    const h = el('div', 'form-hint', text);
    h.style.marginLeft = '138px';     // 对齐输入列（label 固定 130px + gap 8px）
    return h;
  }

  /* 标题旁带圈的 i：鼠标移入（或点击/回车）即展开；鼠标**留在 ⓘ 或提示框里**就一直显示，
     移到这块区域之外就自动收起（也按 Esc 可收起，键盘用户没有「移出」这个机会）。
     为什么不是简单 mouseleave 就收：ⓘ 和提示框之间隔着 8px 空隙，
     鼠标往下移进框里时会先触发一次「移出」，于是给一个很短的宽限期，
     只要在这段时间内进到框里，就算没离开过。 */
  const TIP_HIDE_DELAY = 300;
  let _escBound = false;
  function infoTip(hint) {
    const wrap = el('span', 'info-tip');
    const icon = el('span', 'info-tip-icon', 'i');
    icon.setAttribute('role', 'button');
    icon.tabIndex = 0;
    icon.title = '查看前提条件';
    icon.setAttribute('aria-label', hint.title || '前提条件');
    wrap.appendChild(icon);

    const bubble = el('div', 'info-tip-bubble');
    const bar = el('div', 'info-tip-bar');
    bar.appendChild(el('span', 'info-tip-title', hint.title || '说明'));
    bubble.appendChild(bar);
    if (hint.code) {
      const pre = el('pre', 'info-tip-code');
      pre.textContent = hint.code;
      bubble.appendChild(pre);
    }
    (hint.notes || []).forEach(n => bubble.appendChild(el('div', 'info-tip-note', '· ' + n)));
    wrap.appendChild(bubble);

    let hideTimer = 0;
    const cancelHide = () => { if (hideTimer) { clearTimeout(hideTimer); hideTimer = 0; } };
    const open = () => {
      cancelHide();
      // 一次只留一个：开新的就把别处还挂着的收掉
      document.querySelectorAll('.info-tip.open').forEach(n => {
        if (n !== wrap) n.classList.remove('open');
      });
      wrap.classList.add('open');
    };
    /* 提示框是 wrap 的子节点，所以鼠标停在框里不会触发 wrap 的 mouseleave */
    wrap.addEventListener('mouseenter', open);
    wrap.addEventListener('mouseleave', () => {
      cancelHide();
      hideTimer = setTimeout(() => {
        hideTimer = 0;
        wrap.classList.remove('open');
      }, TIP_HIDE_DELAY);
    });
    icon.addEventListener('click', e => { e.preventDefault(); open(); });
    icon.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    if (!_escBound) {                 // 全局只绑一次：Esc 关闭所有已展开的提示
      _escBound = true;
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
          document.querySelectorAll('.info-tip.open')
            .forEach(n => n.classList.remove('open'));
        }
      });
    }
    return wrap;
  }

  /* 模块保存控件：脏标记（未保存）＋ 保存按钮。
     - 只有「本模块」相对上次落盘有改动时才可点，避免空保存触发容器重建
     - gate 传入时需先满足条件（服务分组：测试连接通过）才可保存
     - 保存只提交 buildPayload() 的载荷，即只写本模块的键；
       后端也据此只重建这一个服务，label 用于把这层含义说清楚
     - opts（可选）：
         label  按钮文案（模型库是「存入模型库」而不是「保存」）
         save   自定义保存函数，替代 ConfigData.saveModule
         saved  自定义提示/收尾；传了就由它全权负责告诉用户结果
         errTtl 保存失败提示的存活毫秒数（模型 tab 传 3000，让它 3 秒自动关；
                不传 = 报错常驻，得用户点 ✕）
         gateHint 被门禁挡住时按钮上的说明（模型卡片要说的是"先测模型"，
                  不是服务卡片的"先测连接"）
         discard 传了才有「放弃修改」态（模型卡片用）：本模块改了、又被门禁挡着时，
                 这颗按钮此刻能给的唯一动作就是"别存了" —— 与其摆一颗点不动的灰按钮
                 让人反复点，不如换成可点的「放弃修改」，点一下执行这个回调（调用方
                 负责把表单倒回去并重绘，见 ConfigData.restoreModule）。
                 没改动、或门禁已放行时按钮照旧是保存；测通之后自动变回点亮的
                 「更新模型库」，用户不需要额外做什么。
         discardTitle / discardLabel 放弃态的 title 与文案（默认「放弃修改」） */
  function moduleSaveControls(paths, buildPayload, gate, label, opts) {
    opts = opts || {};
    const btnLabel = opts.label || '保存';
    const discardLabel = opts.discardLabel || '放弃修改';
    const tag = el('span', 'module-dirty hidden', '未保存');
    const btn = el('button', 'btn btn-primary btn-sm', btnLabel);
    let saving = false;          // 保存请求在飞：这期间别把"保存中…"改回按钮文案
    /* 此刻这颗按钮代表哪个动作：改了但没过门禁 = 只能"放弃"（discard），
       其余情况都是"保存"。判定只读当下的脏标记与门禁结果，所以点过「测试模型」
       之后同一次渲染里它就自己变回「更新模型库」了（save.refresh() 由测试按钮调） */
    const mode = () => (opts.discard && ConfigData.moduleDirty(paths)
      && !!gate && !gate()) ? 'discard' : 'save';
    const refresh = () => {
      const changed = ConfigData.moduleDirty(paths);
      const blocked = !!gate && !gate();
      const discarding = mode() === 'discard';
      tag.classList.toggle('hidden', !changed);
      // 放弃态是"可点的次要动作"：次级灰（btn-secondary），不是点亮的蓝色主按钮
      btn.className = discarding ? 'btn btn-secondary btn-sm'
                                 : 'btn btn-primary btn-sm';
      btn.disabled = discarding ? false : (!changed || blocked);
      if (!saving) btn.textContent = discarding ? discardLabel : btnLabel;
      // 门禁优先说：模型卡片一进页面就是"没改动 + 没测通"，若只说"没有未保存的
      // 修改"，用户点不动按钮却看不到真正的原因（得先测连接 / 测模型）
      btn.title = discarding
        ? (opts.discardTitle || '放弃本模块未保存的修改')
        : blocked
          ? (opts.gateHint || '先测试连接，通过后才能保存')
            + (changed ? '' : '；本模块当前也没有未保存的修改')
          : !changed ? '本模块没有未保存的修改'
            : (opts.buttonTitle || '只保存本模块的配置');
    };
    btn.addEventListener('click', async () => {
      if (mode() === 'discard') {
        /* 「放弃修改」：按钮此刻不代表"存下去"，只代表"不存了"。倒回哪一套由
           discard 回调决定（模型卡片上是回到基线：生效的那套 / 点开的那条条目） */
        btn.disabled = true;
        try {
          opts.discard();
        } finally {
          refresh();     // 回调若没重绘（面板重绘会把本按钮换掉），这里兜底复位
        }
        return;
      }
      btn.disabled = true;
      saving = true;
      btn.textContent = '保存中…';
      try {
        const payload = buildPayload();
        const res = opts.save ? await opts.save(payload)
          : await ConfigData.saveModule(payload, paths);
        if (opts.saved) {
          opts.saved(res);
        } else {
          const who = label || '本模块';
          const names = Object.keys((res && res.degraded) || {});
          const scoped = !!(res && res.scope && res.scope !== 'container');
          if (res && res.applied === false) {
            // 写进文件了但没能热应用：如实说，别让用户以为已经生效
            showToast(res.message || (who + ' 已保存，但热应用失败（重启后生效）'),
                      'warning');
          } else if (names.length) {
            showToast(who + ' 已保存；仍处降级：'
              + names.map(degradedName).join('、'), 'warning');
          } else {
            showToast(scoped ? who + ' 已保存并重连，未影响其它服务'
                             : who + ' 已保存并热应用', 'success');
          }
          Partials.refreshPiece('panel_system');
        }
      } catch (e) {
        // 校验类错误本身就是完整提示，不再套"保存失败："前缀
        // （模型 tab 的调用方传 errTtl，让这条报错 3 秒自动关；不传 = 常驻）
        showToast(e.validation ? e.message : '保存失败：' + e.message, 'error',
                  opts.errTtl);
      } finally {
        saving = false;
        refresh();     // 文案与可点状态都由它按当下情况复位（含"改了但没测通"→放弃态）
      }
    });
    refresh();   // 建好即按基线判定一次：没改过就不该能点，免得空保存触发容器重建
    return { tag, btn, refresh };
  }

  function textInput(value, onChange, opts) {
    const i = el('input', 'form-input');
    i.value = value == null ? '' : String(value);
    if (opts && opts.type) i.type = opts.type;
    if (opts && opts.placeholder) i.placeholder = opts.placeholder;
    i.addEventListener('input', () => onChange(i.value));
    return i;
  }

  /* 凭据框里的值 → 后端要的语义。框里摆的东西分两类（见 routes._param_row）：
       · api_key：框里就是**解密后的真值**（密码框画成圆点而已）→ 原样提交。
         它就是"这次要用、要存的那把钥匙"，删坏一个字符就是真坏了，
         测试会如实失败 —— 不再有"看着像改过、其实回落到了自己的钥匙"这种假象。
       · 其余凭据：框里那串圆点是**纯显示**的占位（不是真值），必须还原：
           - 还是那串圆点（或只剩几个点）= 用户没动过 → 留空，后端按"沿用这条
             自己的钥匙"回落（见 routes._effective_api_key）
           - 空串 = 用户主动清空 → cleared，后端把这条的 key 置空
           - 其它 = 用户新填的钥匙 → 原样提交
         圆点串永远不可能是真钥匙，所以"只删掉几个点"这种中间态也归到"没动过"，
         宁可当没改，也不能把一串圆点写进配置。

     raw = 框里**当前**的文本（调用点直接读输入框，见 secretInputs）。
     渲染刻意不把圆点写回 p.value：那样一来"每次重绘"都会被当成一次改动，
     模块立刻显示「未保存」，圆点串还会顺着载荷流进配置。所以 p.value 单看
     分不出「没动过」与「被删空」（都是空串），必须以框里现读的 raw 为准。 */
  function secretFormValue(p, raw) {
    p = p || {};
    const dots = (p.hasValue && typeof p.valueLen === 'number')
      ? '•'.repeat(p.valueLen) : '';
    const v = String(raw != null ? raw : (p.value == null ? '' : p.value));
    if (v === dots || (v !== '' && /^•+$/.test(v))) {
      return { value: '', cleared: false };   // 没动过
    }
    if (v === '') return { value: '', cleared: dots !== '' };   // 主动清空
    return { value: v, cleared: false };      // 真值 / 新钥匙：原样提交
  }

  /* 拿回来的候选模型：徽标直接排在按钮后面，点一个就填进「模型ID」框（并标脏）。
     这只是"服务端自报有哪些模型"，用来省手抄，不是白名单 —— 模型ID 始终允许手填。
     徽标上刻意不给「×」：真正持久化的只有「模型ID」那一个值，候选列表还没落盘，
     给删除键只会让人以为"删掉就等于没配"。 */
  function attachModelChips(box, models, getCur, setInput) {
    const items = models.slice();
    const redraw = () => {
      box.innerHTML = '';
      if (!items.length) {
        box.appendChild(el('span', 'form-hint', '服务端没返回模型：直接手填「模型ID」'));
        return;
      }
      const cur = (getCur() || '').trim();
      items.forEach(name => {
        const chip = el('button', 'model-chip' + (name === cur ? ' active' : ''));
        chip.type = 'button';
        chip.title = '填入「模型ID」：' + name;
        chip.appendChild(el('span', 'model-chip-name', name));
        chip.addEventListener('click', () => {
          if (setInput) setInput(name);
          redraw();     // 选中的那个高亮：与「模型ID」框里是同一个值
        });
        box.appendChild(chip);
      });
    };
    redraw();
  }

  /* bool 型参数的两种取值文案。不能一律写 "是/否"：
     「启用状态」惯用 Enable/Disable，「校验证书」要说清校验 / 不校验 */
  const BOOL_OPTION_LABELS = {
    enabled: ['Enable', 'Disable'],
    verify_certs: ['校验', '不校验'],
  };
  const BOOL_OPTION_FALLBACK = ['是', '否'];

  /* bool 型参数的字段级说明：直接铺在单选行下方。
     塞进 ⓘ 气泡够不到 "选这一项会怎样" 这种当场决策（verify_certs 尤其如此：
     它在 http:// 地址下选什么都没有区别，光看 "校验证书" 四个字看不出来） */
  const BOOL_FIELD_HINTS = {
    verify_certs: '证书仅对 https 地址生效',
  };

  /* 改这些字段不必重测模型：它们不参与"连得上吗、这个模型能用吗" ——
     模型名只是卡片上的标识，温度 / 最大 Token 是生成参数，向量维度 / 查询前缀是
     检索侧的用法。改完直接可存，否则用户只想调个温度却被逼着再打一次模型。
     其余字段（API 地址 / API Key / 模型ID 等）任一改动都会让上一次测试不再为当前
     值背书 —— 模型ID 尤甚，那正是「测试模型」要验证的东西。 */
  const TEST_IRRELEVANT_KEYS = ['display_name', 'temperature', 'max_tokens',
                                'dim', 'query_prefix'];

  /* 单选行（如 CPU/GPU 设备选择） */
  function radioRow(labelText, name, options, value, onChange) {
    const row = el('div', 'perm-row');
    const lab = el('label', 'form-label');
    lab.style.minWidth = '130px';
    lab.style.margin = '0';
    lab.textContent = labelText;
    row.appendChild(lab);
    const wrap = el('div', 'radio-group');
    const uid = name + '-' + Math.random().toString(36).slice(2, 7);
    options.forEach(o => {
      const id = 'rd-' + uid + '-' + o.v;
      const r = el('input');
      r.type = 'radio';
      r.name = uid;
      r.value = o.v;
      r.id = id;
      if (value === o.v) r.checked = true;
      r.addEventListener('change', () => { if (r.checked) onChange(o.v); });
      const l = el('label', 'radio-opt');
      l.setAttribute('for', id);
      l.appendChild(r);
      l.appendChild(el('span', null, o.label));
      wrap.appendChild(l);
    });
    row.appendChild(wrap);
    return row;
  }

  /* ══ 模型库（模型 tab 左列）══════════════════════════════════
     "配好一个就覆盖掉上一个"是原来的行为：用户手上有线上 DeepSeek、内网 vLLM、
     本机 Ollama 时，换回来就得重填地址和 key。这里把每个「已测通」的配置列出来，
     active 只决定业务用哪一套，切换不重填、不丢 key（后端 models._sync_model_library）。 */

  /* 左列里「被点中的那条」的 id（按段）。选中 = 右侧表单现在显示的是它、入库时会
     更新它（没选中则是新增一条）。放这里而不是挂在 g 上：g 是回传给后端的配置载荷，
     掺进 _editingId 这类纯界面状态会被一起写进配置文件 */
  const modelEdit = { llm: '', vlm: '', embedding: '', rerank: '' };

  function libButton(text, cls, title, onClick) {
    const b = el('button', cls, text);
    b.type = 'button';
    if (title) b.title = title;
    b.addEventListener('click', onClick);
    return b;
  }

  /* 按钮级异步动作：请求期间禁用并显示进度，失败把后端原话抛给用户 */
  async function libAction(btn, fn) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
      await fn();
    } catch (e) {
      // 模型库按钮只在模型 tab 上：报错 3 秒自动关
      showToast(e.validation ? e.message : '操作失败：' + e.message, 'error',
                MODEL_ERR_TTL);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  const entryRowValue = (entry, key) => {
    const row = (entry.configParams || []).find(p => p.key === key);
    return row ? String(row.value == null ? '' : row.value) : '';
  };

  /* 条目 → 表单：逐行覆盖值，标签/类型/是否凭据沿用现有行。
     条目少一行（历史条目缺项）就保留表单现值，免得点一下卡片输入框平白少一个 */
  function backfillFromEntry(g, entry) {
    const cur = {};
    (g.configParams || []).forEach(p => { cur[p.key] = p; });
    const rows = [];
    (entry.configParams || []).forEach(p => {
      const old = cur[p.key];
      rows.push(old ? Object.assign({}, old, {
        value: p.value, hasValue: p.hasValue, valueLen: p.valueLen,
        secret: p.secret,
      }) : p);
    });
    (g.configParams || []).forEach(p => {
      if (!rows.some(r => r.key === p.key)) rows.push(p);
    });
    g.configParams = rows;
    // 重排模型是本机权重，表单里没有「API 地址」这一行（见 noEndpoint），
    // 也就没有 endpoint 可回填 —— 照旧给 g 塞一个空串反而会凭空长出一行
    if (!g.noEndpoint) g.endpoint = entry.endpoint;
    // 只取第一个：它是这条配置调用的那个，也是「模型ID」框该显示的值
    // （老条目里可能存着多个，界面不再摆出其余那些）
    g.modelIds = (entry.modelIds || []).slice(0, 1);
  }

  /* 表单回到"当前生效的配置"（取消选中时用） */
  function restoreFormFromActive(g) {
    const entries = (g.library || {}).entries || [];
    const act = entries.find(e => e.active) || entries[0];
    if (!act) return;
    if (!g.noEndpoint) g.endpoint = act.endpoint;
    g.modelIds = (act.modelIds || []).slice(0, 1);
    g.configParams = JSON.parse(JSON.stringify(act.configParams || []));
  }

  /* 「增加模型」：把右侧表单清成一张空白表 —— 行都留着（标签/类型/是否凭据沿用
     原有那套），只是值全空，于是接下来填的是一套**新**配置，入库走"新增"而不是
     覆盖某一条（有无 id 决定新增还是更新，见 buildPayload）。
     凭据行的「已存过」标记也一并抹掉：留着它，api_key 框会摆出上一条钥匙长度的
     圆点，看着像还在用旧钥匙（圆点占位见 secretFormValue）。
     清完把基线对齐到这张空表：清空不算"改动"，「未保存」不该亮起来，真填了字段
     才由 touch() 点亮。 */
  function clearFormForNew(g) {
    (g.configParams || []).forEach(p => {
      p.value = '';
      p.hasValue = false;
      p.valueLen = 0;
    });
    if (!g.noEndpoint) g.endpoint = '';
    g.modelIds = [];
  }

  /* 模型库操作后的统一提示：核心是"生效没生效"，别让用户以为切过去了 */
  function modelSavedToast(wrapped, opts) {
    const res = (wrapped && wrapped.res) || {};
    const names = Object.keys(res.degraded || {});
    const name = (opts && opts.name) || '';
    if (res.applied === false) {
      showToast(res.message || '已写入模型库，但热应用失败（重启后生效）', 'warning');
    } else if (names.length) {
      showToast('模型库已更新；仍处降级：' + names.map(degradedName).join('、'),
                'warning');
    } else if (opts && opts.activated) {
      showToast('已生效：' + name + '（问答等业务立即改用这一套）', 'success');
    } else if (opts && opts.deleted) {
      showToast('已从模型库删除：' + name, 'success');
    } else {
      showToast('已存入模型库：' + name + '；点「设为 active」让它生效', 'success');
    }
    Partials.refreshPiece('panel_system');   // 降级状态可能变了
    Partials.refreshPiece('panel_model');    // 左列表 + 右表单都要重绘
  }

  /* 左列标题带上槽位名：三块模型库上下叠着，都只写「模型库」就分不清谁是谁
     （槽位名就是后端那套段名的 key；重排段虽挂在 retrieval 上，但接口层用
     routes._RerankStore 伪装成了同样的"段"，这里是同一个标题口径） */
  const LIB_TAGS = { llm: 'LLM', vlm: 'VLM', embedding: 'Embedding',
                     rerank: 'Rerank' };
  const libTitle = key => (LIB_TAGS[key] ? '模型库 · ' + LIB_TAGS[key] : '模型库');

  /* ── 右列表单的"共用"：LLM 与 VLM 两块模型库只留一份填空框 ──
     这两段本来就是同一种东西（OpenAI 兼容的对话服务：地址 + Key + 模型ID + 一组
     运行参数），差别只在业务上谁去吃图。若各出一张卡片、各带一份一模一样的表单，
     用户分不清该填哪一个、写进哪一段。所以 VLM 只作为左列的第二个库挂在 LLM 卡片
     里（见 modelCard），右边只有一份表单：**点哪一块的卡片，表单就是编哪一段**，
     段名跟着切（modelEdit、markSaved 的键、入库的 section 全是它）。
     不是"两份表单轮流显隐"——那样填到一半的值会在切换时凭空消失。
     表单标题对这两段统一写「大模型」（后端段标签仍是「LLM 大模型」「VLM 视觉
     模型」，只用在报错与灰牌上），另挂一枚段名徽标说明此刻在编哪一段。 */
  const SHARED_FORM_LIBS = ['llm', 'vlm'];
  const SHARED_FORM_OWNER = 'llm';        // 卡片挂在谁身上（VLM 不自出一张卡）
  const SHARED_FORM_TITLE = '大模型';
  let sharedFormKey = SHARED_FORM_OWNER;  // 此刻表单在编哪一段
  const isSharedLib = key => SHARED_FORM_LIBS.indexOf(key) >= 0;

  /* 挂在同一张卡片上的库：owner → [自己, VLM]，其余段 → [自己] */
  function libsOfCard(g) {
    if (g.key !== SHARED_FORM_OWNER) return [g];
    return [g].concat((ConfigData.groupsFor('model') || []).filter(
      x => x !== g && isSharedLib(x.key)));
  }

  /* 此刻表单该编的那一段（点过 VLM 卡片就是 VLM，否则是卡片本来那一段） */
  function sharedFormGroup(g) {
    return libsOfCard(g).find(x => x.key === sharedFormKey) || g;
  }

  function renderLibrary(g) {
    const lib = g.library || { activeId: '', entries: [] };
    const entries = lib.entries || [];
    const box = el('div', 'model-lib');
    const head = el('div', 'model-lib-head');
    head.appendChild(el('span', 'model-lib-title', libTitle(g.key)));
    head.appendChild(el('span', 'badge badge-default', entries.length + ' 个已测通'));
    /* 「增加模型」贴在这一行的最右边：点一下 = 右侧表单清空 + 退出选中态，
       于是接下来填的是一套新配置（入库走新增）；不点它也能直接改现有条目。
       没有条目时它也照旧摆着 —— 总得有个"从空白开始填"的入口 */
    const add = libButton('增加模型', 'btn btn-ghost btn-sm',
      '清空右侧表单，用来配一个新模型（测通后点「存入模型库」）', () => {
        // 共用一份表单时（LLM / VLM）：先把表单切到本段，再清空 —— 否则清的是
        // 另一段的值，看着像"点了没反应"
        if (isSharedLib(g.key)) sharedFormKey = g.key;
        modelEdit[g.key] = '';
        clearFormForNew(g);
        ConfigData.markSaved(['model:' + g.key]);   // 清空不是改动
        Partials.refreshPiece('panel_model');
      });
    add.style.marginLeft = 'auto';   // 靠这一行最右
    head.appendChild(add);
    box.appendChild(head);

    if (!entries.length) {
      const empty = el('div', 'model-lib-empty');
      empty.appendChild(el('div', null, '还没有已测通的模型'));
      empty.appendChild(el('div', 'form-hint',
        '在右侧填好参数（模型ID 可点「获取模型ID」从服务端列表里选）'
        + ' → 点「测试模型」→ 通过回应后点「存入模型库」'));
      box.appendChild(empty);
      return box;
    }

    entries.forEach(e => {
      const selected = modelEdit[g.key] === e.id;
      const item = el('div', 'model-lib-item'
        + (e.active ? ' active' : '') + (selected ? ' selected' : ''));
      const line = el('div', 'model-lib-line');
      line.appendChild(el('span', 'model-lib-name', e.displayName));

      /* 标记位：active 是绿牌，不是 active 时这里就是「设为 active」。点完列表
         重绘，同一个位置变成绿牌 —— 标记与按钮共用一个槽，卡片不会跳。
         地址与 key 不进列表：它们占行宽又不常看，点卡片即可回填到右侧核对 */
      const acts = el('div', 'model-lib-acts');
      if (e.active && g.notEnabled) {
        /* 重排模型特有：这一条确实是 active，但「检索策略」里的「启用重排」没勾上，
           它压根不参与召回 —— 绿牌 active 在这里就成了谎话，换成灰牌说明原因
           （文案由后端给，见 _normalize_config 的 notEnabled） */
        const b = el('span', 'badge badge-default', '未启用');
        b.title = g.notEnabled;
        acts.appendChild(b);
      } else if (e.active) {
        acts.appendChild(el('span', 'badge badge-success', 'active'));
      } else {
        // 设为 active：只改"业务用哪一套"，条目本身不动
        acts.appendChild(libButton('设为 active', 'badge badge-btn',
          '让问答等业务改用它（立即热应用）',
          ev => libAction(ev.currentTarget, async () => {
            const w = await ConfigData.activateModel(g.key, e.id);
            if (w.res && w.res.noop) {
              showToast('「' + e.displayName + '」已经是当前生效的模型', 'info');
              Partials.refreshPiece('panel_model');
              return;
            }
            modelSavedToast(w, { name: e.displayName, activated: true });
          })));
      }

      const del = libButton('删除', 'btn btn-ghost btn-sm model-lib-del',
        e.active ? '当前生效的模型不能删除：请先把别的设为 active'
                 : '从模型库移除这一条（不影响正在生效的模型）',
        ev => {
          if (e.active) {
            showToast('「' + e.displayName + '」是当前生效的模型，不能删除；'
              + '请先把别的模型设为 active', 'warning', MODEL_ERR_TTL);
            return;
          }
          if (!window.confirm('从模型库删除「' + e.displayName + '」？\n\n'
            + '只删掉这一条配置，不影响正在生效的模型。')) return;
          libAction(ev.currentTarget, async () => {
            if (modelEdit[g.key] === e.id) modelEdit[g.key] = '';
            const w = await ConfigData.deleteModel(g.key, e.id);
            modelSavedToast(w, { name: e.displayName, deleted: true });
          });
        });
      acts.appendChild(del);
      line.appendChild(acts);
      item.appendChild(line);

      /* 点卡片 = 把这一条回填到右侧表单（原先卡片上那个「更改」按钮干的事）：
         要改哪一项就在右边改，改完照旧「测试模型」→「更新模型库」。
         选中态只在卡片上描一圈蓝框（见 .model-lib-item.selected），卡上不写
         "编辑中"、右侧也不摆横幅与「取消编辑」—— 退出选中就再点一下这张卡片，
         表单回到当前生效的那套。
         回填后立刻把本模块的基线对齐到这条配置：只是"选中看一眼"不算改动，
         「未保存」不该因为点了一下卡片就亮起来（真改了字段才由 touch() 点亮） */
      item.classList.add('clickable');
      item.title = selected
        ? '已选中（右侧表单就是它）；再点一下取消选中'
        : '点一下：把这条配置回填到右侧表单'
          + '（改完测试通过后点「更新模型库」）';
      item.addEventListener('click', ev => {
        if (ev.target.closest('button')) return;   // 卡上的按钮各管各的
        // 共用一份表单时（LLM / VLM）：表单切到本段。点 VLM 的卡片就是在编 VLM，
        // 点 LLM 的就是编 LLM —— 段名（modelEdit 的键、入库的 section）始终是 g.key
        if (isSharedLib(g.key)) sharedFormKey = g.key;
        if (selected) {
          modelEdit[g.key] = '';
          restoreFormFromActive(g);
        } else {
          modelEdit[g.key] = e.id;
          backfillFromEntry(g, e);
        }
        ConfigData.markSaved(['model:' + g.key]);
        Partials.refreshPiece('panel_model');
      });

      /* 卡片上只摆一个模型ID：就是这条配置调用的那个（表单里「模型ID」框里的值，
         后端条目里的 model_ids[0]）。它和模型名一起，是区分两条配置的全部信息 ——
         地址、key 都得点卡片回填才看得到，卡片才排得下多条 */
      const inUse = (e.modelIds && e.modelIds[0]) || entryRowValue(e, 'model');
      if (inUse) {
        const chips = el('div', 'model-lib-chips');
        const c = el('span', 'model-chip static active');
        // 是不是业务在用的还取决于这条是否 active
        c.title = inUse + (e.active ? '（业务正在用它）' : '（这条配置调用它）');
        c.appendChild(el('span', 'model-chip-name', inUse));
        chips.appendChild(c);
        item.appendChild(chips);
      }
      box.appendChild(item);
    });
    return box;
  }

  /* 模型卡片 = 左列模型库 + 右列配置表单
     表单缩到右边而不是占满整宽：它只是"填参数、测试、入库"的入口，
     真正要经常看的是左边"有哪些已测通的、当前用哪个"。 */
  function modelCard(g) {
    const body = el('div', 'model-card-body');
    const libs = libsOfCard(g);
    /* 一块库就照旧直接摆进左列；两块（LLM + VLM）要先套一层列容器 ——
       它们是同一列里上下叠着的两块 .model-lib，不是网格里的两个格子 */
    const box = libs.length > 1 ? el('div', 'model-lib-col') : body;
    libs.forEach(x => box.appendChild(renderLibrary(x)));
    if (box !== body) body.appendChild(box);

    const right = el('div', 'model-form-col');
    // 表单跟着"最后点的那块库"走；只有一块库的卡片就是它自己
    const fg = libs.length > 1 ? sharedFormGroup(g) : g;
    right.appendChild(groupCard(fg, libs.length > 1 ? SHARED_FORM_TITLE : ''));
    body.appendChild(right);
    return body;
  }

  /* ── 模型/服务/高级组卡片 ── */
  function groupCard(g, titleOverride) {
    const card = el('div', 'card perm-card');
    const head = el('header');
    const titleWrap = el('div', 'card-title');
    // 共用一份表单的两段（LLM / VLM）标题都写「大模型」（见 SHARED_FORM_TITLE）；
    // 各自单独出卡片时（理论上的兜底）仍用后端给的那段标签
    titleWrap.appendChild(el('span', 'doc-title-main',
      titleOverride || g.label || g.key));
    /* 同一份表单能编两段，标题又统一写「大模型」，就得有东西说清此刻写进去的是
       哪一段的库 —— 这枚徽标就是那个说明（点另一块库的卡片它会跟着变） */
    if (titleOverride) {
      titleWrap.appendChild(el('span', 'badge badge-default',
        LIB_TAGS[g.key] || g.key));
    }
    /* 「模型名」的牌子：只有 LLM / 向量模型卡片有这一项（后端 _MODEL_SECTIONS）。
       名字写在参数行里，身份却必须挂在标题上 —— 否则换了一套模型服务，两张卡片的
       标题一模一样，只有逐行读参数才认得出配的是谁。这里从 configParams 里取同一份
       值（不另存一份），改一个字符标题就跟着变，用户才不会怀疑没保存上。 */
    const nameParam = (g.configParams || []).find(p => p.key === 'display_name');
    const nameTag = nameParam ? el('span', 'doc-title-sub', '') : null;
    if (nameTag) {
      nameTag.title = '模型名（仅用于区分不同配置，不影响连接）';
      titleWrap.appendChild(nameTag);
    }
    const syncNameTag = () => {
      if (!nameTag) return;
      const v = String(nameParam.value || '').trim();
      nameTag.textContent = v ? '· ' + v : '';
      nameTag.classList.toggle('hidden', !v);
    };
    syncNameTag();
    if (g.hint) titleWrap.appendChild(infoTip(g.hint));
    if (g.refresh === 'restart') {
      titleWrap.appendChild(el('span', 'badge badge-warning', '需重启'));
    }
    head.appendChild(titleWrap);

    const scope = ConfigData.groupScope(g);
    const isModel = scope === 'model';
    const path = ConfigData.groupPath(g);
    // 模型段必须测通才能入库（后端同样校验）：库里只该有"连得上"的配置
    const needTest = isModel || g.saveGate === 'test';
    const actions = el('div', 'card-actions');
    let tested = false;         // 本卡片当前表单值是否已测通

    /* 保存只提交本卡片所在的这一个分组 */
    const buildPayload = () => {
      if (isModel) {
        // 模型名必填：卡片标题上显示的就是它，空名字等于两张卡片又分不出来了。
        // 与 API 地址无关，本机/Mock 模式同样要有名字
        const nameRow = (g.configParams || []).find(p => p.key === 'display_name');
        const name = nameRow ? String(nameRow.value || '').trim() : '';
        if (!name) {
          const err = new Error((g.label || g.key) +
            '：模型名必填 —— 它就是卡片标题上那个名字，用来区分不同的模型配置');
          err.validation = true;
          throw err;
        }
        // 入库的模型ID只有一个 = 表单里「模型ID」框里那个（后端条目里的 model
        // 是它的镜像）。「获取模型ID」拉回来的候选只是填充手段，不进配置
        const modelRow = (g.configParams || []).find(p => p.key === 'model') || {};
        const inUse = String(modelRow.value == null ? '' : modelRow.value).trim();
        // 重排模型是本机权重（noEndpoint）：没有服务地址可填，也不该拦住入库
        const ep = g.noEndpoint ? '' : String(g.endpoint || '').trim();
        if (!g.noEndpoint && !ep) {
          const err = new Error((g.label || g.key) +
            '：API 地址必填 —— 留空是本机 Mock 模式，没有可入库的配置');
          err.validation = true;
          throw err;
        }
        // 重排没有「API 地址」这一行，地址那一半并进了「模型路径/API」——
        // 它就是这段的必填项（后端同样校验）：填目录 = 本机权重，填 http(s) 地址 =
        // 远程重排服务。留空等于这条配置谁也重排不了，而用户以为配好了
        const dirRow = (g.configParams || []).find(p => p.key === 'model_dir');
        if (dirRow && !String(dirRow.value == null ? '' : dirRow.value).trim()) {
          const err = new Error((g.label || g.key) +
            '：模型路径/API 必填 —— 填本机权重目录（如 models），' +
            '或远程重排服务地址（http://host:port/v1）');
          err.validation = true;
          throw err;
        }
        if (!inUse) {
          const err = new Error((g.label || g.key) +
            '：模型ID必填 —— 直接手填，或点上面的「获取模型ID」从返回的列表里选用');
          err.validation = true;
          throw err;
        }
        // 入库载荷：带 id = 更新那一条，不带 = 新增。
        // activate 只在"改的就是当前生效那一条"时置真 —— 改它就是要它生效；
        // 新增一条 / 改别的条目都不该顺手把业务切走（界面另有「设为 active」）
        const editingId = modelEdit[g.key] || '';
        // 凭据行提交前按框里现读的文本还原后端语义：api_key 框里就是真钥匙
        // （原样提交），其余凭据的圆点占位 = 没动过（留空沿用这条自己的那把），
        // 删干净 = cleared（主动清空），填了新的 = 新值（见 secretFormValue）
        const params = (g.configParams || []).map(p => {
          if (!p.secret) return p;
          const s = secretFormValue(p, (secretInputs[p.key] || {}).value);
          return Object.assign({}, p, { value: s.value, cleared: s.cleared });
        });
        return {
          id: editingId,
          endpoint: ep,
          modelIds: [inUse],     // 只有框里那个：这条配置就调用它
          configParams: params,
          verified: true,        // 走到这里必然测过：按钮被 needTest 的 gate 挡住
          activate: !!editingId && editingId === (g.library || {}).activeId,
        };
      }
      return ConfigData.groupPayload(g);
    };
    const save = moduleSaveControls(path, buildPayload,
      () => !needTest || tested, g.label || g.key,
      isModel ? {
        label: modelEdit[g.key] ? '更新模型库' : '存入模型库',
        buttonTitle: '只把这段模型配置存入模型库，不影响其它服务',
        errTtl: MODEL_ERR_TTL,     // 入库失败：报错 3 秒自动关
        // 挡住的是哪一步：模型卡片要过了「测试模型」才能真正调用
        gateHint: '先「测试模型」，有正确回应后才能保存',
        /* 改了模型参数（地址 / Key / 模型ID…）又还没重新测通时，那颗灰着的
           「更新模型库」换成可点的「放弃修改」—— 此刻保存做不了，按钮能给的
           唯一动作就是"别存了"。点一下把表单倒回基线（当前生效的那套，或点开的
           那条库条目；ConfigData.restoreModule 只动这一个模块），「未保存」随之
           消掉；测过之后它自然变回点亮的「更新模型库」。 */
        discardTitle: '放弃这次未保存的修改，表单回到已保存的那一套',
        discard: () => {
          ConfigData.restoreModule(path);
          showToast((g.label || g.key) + '：已放弃未保存的修改', 'info',
                    MODEL_ERR_TTL);
          Partials.refreshPiece('panel_model');   // 左列表选中态 + 右表单一起重绘
        },
        save: payload => {
          const editingId = modelEdit[g.key] || '';
          const willActivate = !!editingId
            && editingId === (g.library || {}).activeId;
          const name = ((g.configParams || [])
            .find(p => p.key === 'display_name') || {}).value || '';
          return ConfigData.upsertModel(g.key, payload)
            .then(w => {
              modelEdit[g.key] = '';    // 入库即取消选中（表单成了新的"已保存"）
              modelSavedToast(w, { name: String(name).trim(), activated: willActivate });
              return w;
            });
        },
        // 模型库的提示由上面的 save 全权负责（它知道"生效没生效"）
        saved: () => {},
      } : null);

    /* 字段被改动 → 上次那次"测通"不再为当前值背书，保存重新变灰。
       传 key 是为了放行 TEST_IRRELEVANT_KEYS 那几个（改模型名/温度/最大 Token 后
       不必重测）；不带 key 的调用一律按"影响测试"处理 —— 漏传只是多测一次，
       方向安全 */
    const touch = key => {
      if (needTest && tested
          && TEST_IRRELEVANT_KEYS.indexOf(key) < 0) tested = false;
      ConfigData.markDirty();
      save.refresh();
    };

    // 「模型ID」输入框引用（仅模型卡片）：「获取模型ID」的徽标点一下要写回它
    let modelIdInput = null;
    /* 凭据行的输入框引用（按字段名）：提交前要读框里**当前**的文本 ——
       "没动过（框里还摆着圆点）"与"被删空（= 主动清空）"只差那一串点，
       而渲染刻意不往状态里写，所以只能现读框 */
    const secretInputs = {};
    const rowByKey = k => (g.configParams || []).find(p => p.key === k) || {};
    if (g.showTestButton) {
      /* 模型卡片上是「测试模型」：后端会拿地址 + Key + 模型ID 真发一次请求，
         有正确回应才算通过（不是只看地址通不通）。其它服务卡片的「测试连接」
         仍是可达性探测，文案与语义都保持不变 */
      const testLabel = isModel ? '测试模型' : '测试连接';
      const t = el('button', 'btn btn-ghost btn-sm', testLabel);
      t.addEventListener('click', async () => {
        t.disabled = true;
        t.textContent = '测试中…';
        try {
          const ak = secretFormValue(rowByKey('api_key'),
                                    (secretInputs.api_key || {}).value);
          const r = await ConfigData.testConnection(
            g.testKind || g.key, g.endpoint || '',
            ak.value,
            String(rowByKey('model').value || ''),
            g.configParams || [],
            // 选中的那一条：api_key 框里就是这条自己的真钥匙，测的与入库存的
            // 是同一把；万一框里空着，后端也只用这条自己的（不借 active 那条的）
            modelEdit[g.key] || '',
            ak.cleared);
          t.textContent = r.online ? `✓ ${r.latencyMs}ms` : '✗ 失败';
          if (r.online) {
            // 通过：只说"模型可用"，成功提示自动消失
            showToast(r.message || '模型可用', 'success');
            tested = true;
          } else {
            // 不通过：给出具体原因（鉴权 / 模型ID不存在 / 路径不对）；
            // 模型卡片上的报错 3 秒自动关，服务卡片的仍常驻
            showToast(r.message || '测试失败', 'error', isModel ? MODEL_ERR_TTL : 0);
            tested = false;
          }
          save.refresh();
        } catch (e) {
          t.textContent = '✗ 失败';
          showToast('测试失败：' + e.message, 'error', isModel ? MODEL_ERR_TTL : 0);
          tested = false;
          save.refresh();
        } finally {
          setTimeout(() => { t.disabled = false; t.textContent = testLabel; }, 3000);
        }
      });
      actions.appendChild(t);
    }
    actions.appendChild(save.tag);
    actions.appendChild(save.btn);
    head.appendChild(actions);
    card.appendChild(head);

    const rows = el('div', 'perm-rows');
    // 字段顺序 = 用户填写的顺序：模型名 → API 地址 → API Key → 模型ID → 其余参数
    // （后端 _MODEL_SECTIONS 的顺序决定中间几行，见那里）。
    // API 地址不是 configParams 里的一行（它是 g.endpoint 这个独立控件），只能按位置
    // 插进去：跟在"模型名"之后；「获取模型ID」同理插在 api_key 与 model 之间 ——
    // 得先有地址和 Key 才问得出"服务端有哪些模型"。没有模型名的卡片（各服务卡片）
    // 由循环末尾那次调用补上，等于退回原来的"地址行在最前"。
    let endpointDone = false;
    const injectEndpoint = () => {
      // 重排模型（g.noEndpoint）没有独立的「API 地址」行：地址那一半已经并进
      // 「模型路径/API」那一栏（见后端 _RERANK_PARAMS 与 models.is_http_url），
      // 后端也不再下发 endpoint 键。所以这里不画，卡片上就没有那行
      if (endpointDone || g.noEndpoint || g.endpoint === undefined) return;
      endpointDone = true;
      rows.appendChild(kvRow('API 地址', textInput(g.endpoint, v => {
        g.endpoint = v; touch('base_url');
      }, { placeholder: 'http://host:port/v1（留空则降级为内置 Mock）' }), true));
    };
    /* 「获取模型ID」：这一行不是配置项，而是"拿上面填的 API 地址与 API Key（可空）
       去问服务端有哪些模型"的入口 —— 拉回来的模型以徽标列出，点一个就填进下面的
       「模型ID」框。它是省抄手段，不是白名单：模型ID 始终允许手填（vLLM 报的是
       --served-model-name，有的服务压根不实现 /models） */
    let pickerDone = false;
    const injectModelPicker = () => {
      if (pickerDone || !isModel) return;
      pickerDone = true;
      // 重排模型没有服务端可问：这一步是"列一下上面「模型路径/API」那个目录里有
      // 哪些权重目录"，所以按钮名与提示都按本地目录说（后端 list-models 的 rerank
      // 分支）。那一栏填的是远程服务地址时后端只回一句说明 —— 远程不用列本地权重
      const localPick = !!g.noEndpoint;
      const ctl = el('div', 'model-id-pick');
      const btn = el('button', 'btn btn-ghost btn-sm',
                     localPick ? '获取模型' : '获取模型ID');
      btn.type = 'button';
      btn.title = localPick
        ? '列出上面「模型路径/API」目录下的权重目录（每个子目录一个模型），'
          + '点一个即填入「模型ID」；那一栏填的是远程服务地址（http(s)://…）'
          + '则无需获取，模型ID 按服务要求手填'
        : '按上面填的 API 地址与 API Key 要一次可用模型列表；'
          + 'Key 留空 = 用这条已保存的那把';
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = '获取中…';
        try {
          const ak = secretFormValue(rowByKey('api_key'),
                                     (secretInputs.api_key || {}).value);
          const r = await ConfigData.listModels(g.testKind || g.key,
            g.endpoint || '', ak.value,
            g.configParams || [],
            // 同上：圆点没动时用的是选中那条自己的钥匙
            modelEdit[g.key] || '',
            ak.cleared);
          if (!r.ok) {
            // 鉴权不对 / 没有 /models / 连不上：把后端原话给出来，3 秒自动关
            showToast(r.message || '获取失败', 'error', MODEL_ERR_TTL);
            return;
          }
          if (!r.models.length) {
            showToast(r.message || '服务端没返回模型列表：可手填模型ID', 'info');
            return;
          }
          // 候选徽标就排在上面的按钮后面：点一个填进「模型ID」（同一份值也写回
          // model 那一行）。换成别的候选就覆盖它 —— 这里是"改用哪个"，不是攒列表
          attachModelChips(chips, r.models,
            () => String(rowByKey('model').value || ''),
            name => {
              const id = String(name || '').trim();
              if (!id) return;
              g.modelIds = [id];
              const mr = rowByKey('model');
              if (mr) mr.value = id;      // 「测试模型」/ 入库读的是这一行
              if (modelIdInput) modelIdInput.value = id;
              touch('model');             // 换了模型ID → 上次那次测试不再为它背书
            });
          showToast(r.message || '已获取模型列表', 'info');
        } catch (e) {
          showToast('获取失败：' + e.message, 'error', MODEL_ERR_TTL);
        } finally {
          btn.disabled = false;
          btn.textContent = '获取模型ID';
        }
      });
      const head = el('div', 'model-id-pick-head');
      head.appendChild(btn);
      // 候选徽标容器：紧跟在按钮后面（同一行，装不下才换行）
      const chips = el('div', 'model-chip-list');
      head.appendChild(chips);
      ctl.appendChild(head);
      const row = kvRow('可用模型', ctl, false);
      row.style.alignItems = 'flex-start';   // 候选多了会换行，标签顶部对齐
      rows.appendChild(row);
    };
    // configParams（扁平键值，按类型渲染输入框）
    (g.configParams || []).forEach(p => {
      if (p.key !== 'display_name') injectEndpoint();
      if (p.key === 'model' && isModel) injectModelPicker();   // 排在模型ID上方
      // 重排模型的「推理设备」是两选一（cpu / cuda）：理由同下面的 bool —— 后端按
      // 白名单归一，手填 "GPU" 只会静默退回 cpu，那就成了"填了但没生效"
      if (p.key === 'device') {
        rows.appendChild(radioRow(p.label || p.key,
          'svc-device-' + g.key,
          [{ v: 'cpu', label: 'CPU' }, { v: 'cuda', label: 'GPU (CUDA)' }],
          String(p.value || 'cpu').toLowerCase() === 'cuda' ? 'cuda' : 'cpu',
          v => { p.value = v; touch(p.key); }));
        return;
      }
      // bool 型一律给单选，不让人手填 true/false：
      // 后端按字符串判真值（"1 / true / yes / on" 之外都算 false），填错只会静默变 false
      if (p.type === 'bool') {
        const pair = BOOL_OPTION_LABELS[p.key] || BOOL_OPTION_FALLBACK;
        const on = String(p.value).toLowerCase() === 'true';
        rows.appendChild(radioRow(p.label || p.key,
          'svc-bool-' + g.key + '-' + p.key,
          [{ v: 'true', label: pair[0] }, { v: 'false', label: pair[1] }],
          on ? 'true' : 'false',
          v => { p.value = v; touch(p.key); }));
        // 字段级说明另起一行
        if (BOOL_FIELD_HINTS[p.key]) {
          rows.appendChild(hintLine(BOOL_FIELD_HINTS[p.key]));
        }
        return;
      }
      const opts = {};
      let initial = p.value;       // 框里先摆的值（凭据行可能是圆点占位）
      if (p.type === 'int' || p.type === 'float') {
        opts.type = 'number';
        if (p.type === 'float') opts.placeholder = '小数';
      }
      if (p.secret) {
        // 凭据一律用密码框：画出来都是圆点，但框里的**值**分两种（见 routes._param_row）
        //  - api_key：后端下发**解密后的真值** → 框里就是那把钥匙，删掉几个字符
        //    就是真把钥匙改坏了（"测试模型"随之失败）。这样"框里的内容"与
        //    "发出去/存下来的钥匙"始终是同一个东西，不用靠猜。
        //  - 其余凭据：后端不下发真值，只给 hasValue（存过没有）与 valueLen
        //    （存了多长），框里摆**等量圆点**当"已存"的显示（灰字提示会被当成
        //    "没保存上"）。提交前由 secretFormValue 还原成"留空（沿用）/ 清空 /
        //    新值"，后端也把圆点串当"没改"（见 secrets.is_display_dots）。
        // 占位圆点**只进框、不进状态**（不写 p.value / p.dots）：渲染碰配置载荷
        // 的话，每次重绘都会被算成一次改动 —— 模块白亮「未保存」。
        opts.type = 'password';
        if (!p.value && p.hasValue && typeof p.valueLen === 'number') {
          initial = '•'.repeat(p.valueLen);
        }
      }
      if (p.key === 'model' && isModel) {
        /* 模型ID就填在框里，不摆徽标：框里的值 = 这条配置调用的那个
           （= 条目里的 model_ids[0]，后端条目里的 model 是它的镜像）。
           上面那行「获取模型ID」拉回来的徽标，点一下就是往这个框里填值 ——
           省去手抄，但框里始终允许手填（服务端可能报的是别名、或压根不实现
           /models）。候选列表本身不落盘：它是"选哪个"的手段，不是配置内容。
           与其它行一样把值写回 p.value：「测试模型」/ 入库都读这一行 */
        if (!Array.isArray(g.modelIds)) g.modelIds = [];
        const input = textInput((g.modelIds || [])[0] || p.value, v => {
          const t = String(v || '').trim();
          g.modelIds = t ? [t] : [];   // 清空 = 还没填，保存会被必填校验拦下
          p.value = t;
          touch('model');     // 换了模型ID → 上次那次「测试模型」不再为它背书
        }, {
          // 重排的模型ID 是「模型路径/API」下的目录名（本机权重）或服务认的模型名
          // （远程重排），两种都不拖路径 —— 路径/地址那一半在那一行里填；
          // 在线服务的模型ID 才长这样带斜杠的名字
          placeholder: g.noEndpoint ? '权重目录名，如 bge-reranker-base'
                                    : '如 qwen2.5-14b-instruct / bge-m3',
        });
        modelIdInput = input;
        rows.appendChild(kvRow(p.label || p.key, input, true));
        return;
      }
      if (p.key === 'display_name') opts.placeholder = '如：DeepSeek 线上 / 内网 vLLM';
      if (p.key === 'model_dir') {
        // 「模型路径/API」两义（后端按 is_http_url 分流）：占位符把两种写法都摆出来
        opts.placeholder =
          'models（本机权重目录），或 http://host:port/v1（远程重排服务）';
      }
      // api_key 的框里是真值；其余凭据行摆的是圆点占位，状态里仍是空串 ——
      // "没动过"靠提交时现读框里的文本判定（见 secretFormValue / secretInputs）
      const input = textInput(initial, v => {
        p.value = v; touch(p.key);
        // 标题上的牌子与输入框是同一份值，必须同帧刷新
        if (p.key === 'display_name') syncNameTag();
      }, opts);
      if (p.secret) secretInputs[p.key] = input;
      rows.appendChild(kvRow(p.label || p.key, input,
        p.key === 'model' || p.key === 'display_name' || p.key === 'model_dir',
        p.optional));
    });
    injectEndpoint();   // 没有"模型名"的卡片：地址行退回最前
    // paramsJson → JSON 编辑框
    if (g.paramsJson && Object.keys(g.paramsJson).length) {
      const ta = el('textarea', 'form-input');
      ta.rows = Math.min(8, JSON.stringify(g.paramsJson).split('\n').length + 3);
      ta.style.fontFamily = 'var(--font-mono)';
      ta.style.fontSize = 'var(--fs-xs)';
      ta.value = JSON.stringify(g.paramsJson, null, 2);
      ta.addEventListener('change', () => {
        try {
          g.paramsJson = JSON.parse(ta.value || '{}');
          ta.style.borderColor = '';
          touch();
        } catch (e) {
          ta.style.borderColor = 'var(--error)';
          showToast('JSON 格式错误', 'error', isModel ? MODEL_ERR_TTL : 0);
        }
      });
      const lab = el('div', 'field-label-inline', '参数 (JSON)');
      rows.appendChild(lab);
      rows.appendChild(ta);
    }
    card.appendChild(rows);
    return card;
  }

  function renderGroupsPanel(data, cols) {
    // 服务 tab 固定 3 列（cols=3）：模块在服务端的顺序即页面上从左到右、
    // 从上到下的顺序，因此"每行摆哪几个模块"由后端 _SERVICE_SECTIONS 决定。
    const wrap = el('div', cols ? `perm-cards cols-${cols}` : 'perm-cards');
    (data.groups || []).forEach(g => wrap.appendChild(groupCard(g)));
    if (!data.groups || !data.groups.length) {
      wrap.appendChild(el('div', 'table-empty', '该域暂无可配置项'));
    }
    return wrap;
  }

  /* ── 权限映射 ── */
  function renderPermissionsPanel() {
    const c = ConfigData.state.config;
    const wrap = el('div');
    const hint = el('p', 'page-subtitle',
      '角色 → 可访问集合的映射；勾选「管理员」可访问全部集合与配置页。');
    const head = el('div', 'panel-head');
    head.appendChild(hint);
    const save = moduleSaveControls(['permissionMappings'], () => ({
      permissionMappings: ConfigData.state.config.permissionMappings || [],
    }));
    const actions = el('div', 'card-actions');
    actions.appendChild(save.tag);
    actions.appendChild(save.btn);
    head.appendChild(actions);
    wrap.appendChild(head);

    /* 新增/移除角色后按钮仍只在确有改动时可点 */
    const touchAll = () => save.refresh();
    const cards = el('div', 'perm-cards');
    (c.permissionMappings || []).forEach((p, idx) => {
      const card = el('div', 'perm-card');
      const rows = el('div', 'perm-rows');
      rows.appendChild(kvRow('角色', textInput(p.role, v => {
        p.role = v; touchAll();
      })));
      rows.appendChild(kvRow('集合(逗号分隔)', textInput((p.collections || []).join(','), v => {
        p.collections = v.split(',').map(s => s.trim()).filter(Boolean);
        touchAll();
      }, { placeholder: '* 或 collection 名' })));
      const adminRow = el('label', 'path-toggle' + (p.is_admin ? ' on' : ''));
      adminRow.innerHTML = '<input type="checkbox" style="display:none"><span>管理员</span>';
      adminRow.addEventListener('click', e => {
        e.preventDefault();
        p.is_admin = adminRow.classList.toggle('on');
        touchAll();
      });
      rows.appendChild(adminRow);
      card.appendChild(rows);
      const del = el('button', 'btn btn-ghost btn-xs text-error', '移除该角色');
      del.addEventListener('click', () => {
        c.permissionMappings.splice(idx, 1);
        ConfigData.markDirty();
        Partials.refreshPiece('panel_permissions');
      });
      card.appendChild(del);
      cards.appendChild(card);
    });
    wrap.appendChild(cards);
    const add = el('button', 'btn btn-secondary btn-sm', '＋ 添加角色映射');
    add.style.marginTop = '12px';
    add.addEventListener('click', () => {
      c.permissionMappings = c.permissionMappings || [];
      c.permissionMappings.push({ role: '', collections: ['*'], is_admin: false });
      ConfigData.markDirty();
      Partials.refreshPiece('panel_permissions');
    });
    wrap.appendChild(add);
    return wrap;
  }

  /* 重排模型块已并入上面那套通用模型卡片：库、表单、按钮全由 renderLibrary /
     modelCard 渲染，段描述由后端下发（key='rerank'，见 routes._normalize_config）。
     它与 LLM / 向量两段的差别只有两个开关：noEndpoint（没有**独立的** API 地址行 ——
     地址那一半并进了「模型路径/API」，见 injectEndpoint）、notEnabled（没勾
     「启用重排」时左列给灰牌）。 */

  /* 左列那块的现在由通用 renderLibrary(g) 渲染（含「删除」「设为 active」「增加模型」） */

  /* 右列表单也走通用 modelCard：行由后端 configParams 下发，「推理设备」在下面按
     单选渲染（跟 bool 参数同一个思路），保存按钮是「存入模型库 / 更新模型库」 */

  /* ── 检索策略（附加到模型面板底部） ── */
  function renderRetrievalBlock() {
    const c = ConfigData.state.config;
    const r = (c && c.retrieval) || {};
    const box = el('div', 'card perm-card');
    const head = el('header');
    head.appendChild(el('span', 'doc-title-main', '检索策略'));
    // 重排模型与推理设备由「重排模型」块负责，本块只管权重与阈值
    const paths = ['retrieval.defaultWeights', 'retrieval.rerankEnabled',
                   'retrieval.rerankThreshold'];
    const save = moduleSaveControls(paths, () => ({
      retrieval: {
        defaultWeights: r.defaultWeights || {},
        rerankEnabled: !!r.rerankEnabled,
        rerankThreshold: r.rerankThreshold,
      },
    }), null, null, { errTtl: MODEL_ERR_TTL });
    const touch = () => { ConfigData.markDirty(); save.refresh(); };
    const actions = el('div', 'card-actions');
    actions.appendChild(save.tag);
    actions.appendChild(save.btn);
    head.appendChild(actions);
    box.appendChild(head);

    const wWrap = el('div', 'perm-rows');
    const weights = r.defaultWeights || {};
    Object.keys(weights).forEach(k => {
      wWrap.appendChild(kvRow(k, textInput(weights[k], v => {
        const f = parseFloat(v);
        if (!isNaN(f)) { weights[k] = f; touch(); }
      }, { type: 'number' })));
    });
    const rerank = el('label', 'path-toggle' + (r.rerankEnabled ? ' on' : ''));
    rerank.innerHTML = '<input type="checkbox" style="display:none"><span>启用重排 (rerank)</span>';
    rerank.addEventListener('click', e => {
      e.preventDefault();
      r.rerankEnabled = rerank.classList.toggle('on');
      touch();
      Partials.refreshPiece('panel_model');   // 重排阈值随开关显隐，需重绘
    });
    wWrap.appendChild(rerank);
    if (r.rerankEnabled) {
      wWrap.appendChild(kvRow('重排阈值', textInput(r.rerankThreshold, v => {
        const f = parseFloat(v);
        if (!isNaN(f)) { r.rerankThreshold = f; touch(); }
      }, { type: 'number' })));
    }
    box.appendChild(wWrap);
    return box;
  }

  /* ── 系统面板 ── */
  function renderSystemPanel() {
    const c = ConfigData.state.config;
    const meta = window.__RUNTIME_META__ || {};
    const flags = (c && c.defaultsFlags) || {};
    const wrap = el('div');
    const box = el('div', 'card');
    const body = el('div', 'card-body');
    body.appendChild(el('div', 'doc-title-main', '系统信息'));
    const ul = el('ul', 'doc-attrs');
    [
      ['版本', (c && c.system && c.system.version) || meta.version || '—'],
      ['角色数', String((c && c.system && c.system.roleCount) || 0)],
      ['默认权重已配置', flags.defaultWeights ? '是' : '否（使用内置默认）'],
      ['权限已配置', flags.hasPermissions ? '是' : '否（使用内置默认）'],
      ['LLM 已配置', flags.llmConfigured ? '是' : '否'],
    ].forEach(([k, v]) => {
      const li = el('li');
      li.appendChild(el('span', 'attr-key', k));
      li.appendChild(el('span', 'attr-val plain', v));
      ul.appendChild(li);
    });
    body.appendChild(ul);
    box.appendChild(body);
    wrap.appendChild(box);

    // 降级组件提示（未配置/连接失败的依赖，指导补齐）
    const degraded = (c && c.system && c.system.degraded) || [];
    if (degraded.length) {
      const dbox = el('div', 'card');
      const dbody = el('div', 'card-body');
      dbody.appendChild(el('div', 'doc-title-main', '降级组件（' + degraded.length + '）'));
      // 两类记录共用这张卡片：构造期的"未配置 → 本地替身"与运行期的"连不上了"。
      // 旧文案把两者都说成"已降级为本地实现"，对后者是错的：运行期掉线的段
      // 只是能力暂时关闭，并没有换成替身在跑。
      dbody.appendChild(el('p', 'page-subtitle',
        '以下组件未配置或连接失败：未配置的已自动降级为本地实现，' +
        '此刻连不上的对应能力暂时关闭（服务本身仍可运行）。' +
        '在「模型」「服务」页补齐参数并保存后立即重连；运行期掉线的段由后台自动重试，' +
        '对端恢复即自动恢复，无需重启。' +
        '与连接无关的记录（如「向量空间」）不会随重连消失，需按其说明处置。'));
      const dul = el('ul', 'doc-attrs');
      degraded.forEach(d => {
        const li = el('li');
        li.appendChild(el('span', 'attr-key', degradedName(d.component)));
        li.appendChild(el('span', 'attr-val plain', d.reason || ''));
        dul.appendChild(li);
      });
      dbody.appendChild(dul);
      dbox.appendChild(dbody);
      wrap.appendChild(dbox);
    }

    if (flags.defaultWeights === false || flags.hasPermissions === false) {
      const tip = el('div', 'trash-banner',
        '检测到部分配置使用内置默认值；在「模型」「权限映射」页修改后点该模块的「保存」，' +
        '即可把当前值写入配置文件以接管。');
      tip.style.marginTop = '12px';
      wrap.appendChild(tip);
    }
    return wrap;
  }

  /* ── 注册 piece 渲染与数据源 ── */
  function setup() {
    Partials.registerDataLoader('panel_model', () => ({
      groups: ConfigData.groupsFor('model'),
    }));
    Partials.registerDataLoader('panel_service', () => ({
      groups: ConfigData.groupsFor('service'),
    }));
    Partials.registerDataLoader('panel_special', () => ({
      groups: ConfigData.groupsFor('special'),
    }));
    Partials.registerDataLoader('panel_permissions', () => ({}));
    Partials.registerDataLoader('panel_system', () => ({}));

    Partials.registerPieceRenderer('panel_model', data => {
      // 模型 tab：从上到下几个块（LLM + VLM / Embedding / Rerank），底部为检索策略。
      // 前几块用 modelCard：左列模型库 + 右列配置表单
      const wrap = el('div');
      const stack = el('div', 'model-stack');
      /* 四段模型库（LLM / VLM / 向量 / 重排）都是同一张卡片：段描述由后端下发，
         重排段也在 data.groups 里（key='rerank'）。
         VLM 自己不出卡片 —— 它与 LLM 共用右侧那份表单，作为左列的第二个库挂在
         LLM 卡片里（见 modelCard），「模型库 · VLM」就贴在「模型库 · LLM」下头 */
      const groups = data.groups || [];
      const hasOwner = groups.some(x => x.key === SHARED_FORM_OWNER);
      groups.forEach(g => {
        if (hasOwner && g.key !== SHARED_FORM_OWNER && isSharedLib(g.key)) return;
        stack.appendChild(modelCard(g));
      });
      wrap.appendChild(stack);
      const ret = renderRetrievalBlock();
      ret.style.marginTop = '16px';
      wrap.appendChild(ret);
      return wrap;
    });
    Partials.registerPieceRenderer('panel_service', data => renderGroupsPanel(data, 3));
    Partials.registerPieceRenderer('panel_special', renderGroupsPanel);
    Partials.registerPieceRenderer('panel_permissions', renderPermissionsPanel);
    Partials.registerPieceRenderer('panel_system', renderSystemPanel);
  }

  window.ConfigUI = { setup };
})();
