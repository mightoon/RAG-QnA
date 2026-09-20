/* 配置页渲染：模型/服务/高级/权限/系统 五个面板的 DOM 构建器 */
(function () {
  'use strict';
  const esc = s => window.utils.escapeHtml(s == null ? '' : s);
  const showToast = (msg, type, duration) => {
    if (window.Partials && window.Partials.showToast) {
      window.Partials.showToast(msg, type, duration);   // duration=0 → 常驻，点 ✕ 关闭
    } else console.log('[toast]', type, msg);
  };

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
       后端也据此只重建这一个服务，label 用于把这层含义说清楚 */
  function moduleSaveControls(paths, buildPayload, gate, label) {
    const tag = el('span', 'module-dirty hidden', '未保存');
    const btn = el('button', 'btn btn-primary btn-sm', '保存');
    const refresh = () => {
      const changed = ConfigData.moduleDirty(paths);
      const blocked = !!gate && !gate();
      tag.classList.toggle('hidden', !changed);
      btn.disabled = !changed || blocked;
      btn.title = !changed ? '本模块没有未保存的修改'
        : blocked ? '先测试连接，通过后才能保存'
          : '只保存本模块的配置';
    };
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = '保存中…';
      try {
        const res = await ConfigData.saveModule(buildPayload(), paths);
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
      } catch (e) {
        // 校验类错误本身就是完整提示，不再套"保存失败："前缀
        showToast(e.validation ? e.message : '保存失败：' + e.message, 'error', 0);
      } finally {
        btn.textContent = '保存';
        refresh();
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

  /* 测试连接成功后挂载可选模型列表：点击选用（填入输入框并标脏），× 从列表移除 */
  function attachModelChips(card, models, getCur, setInput) {
    card.querySelectorAll('.model-chip-box').forEach(n => n.remove());
    const box = el('div', 'model-chip-box');
    box.appendChild(el('div', 'field-label-inline', '可用模型（点击选用，× 移除）'));
    const list = el('div', 'model-chip-list');
    box.appendChild(list);
    card.appendChild(box);
    const items = models.slice();
    const redraw = () => {
      list.innerHTML = '';
      if (!items.length) {
        list.appendChild(el('span', 'form-hint', '列表已清空'));
        return;
      }
      const cur = (getCur() || '').trim();
      items.forEach((name, i) => {
        const chip = el('button', 'model-chip' + (name === cur ? ' active' : ''));
        chip.type = 'button';
        chip.title = name;
        chip.appendChild(el('span', 'model-chip-name', name));
        const x = el('span', 'model-chip-x', '×');
        x.title = '从列表移除';
        x.addEventListener('click', ev => {
          ev.stopPropagation();
          items.splice(i, 1);
          redraw();
        });
        chip.appendChild(x);
        chip.addEventListener('click', () => {
          if (setInput) setInput(name);
          redraw();
        });
        list.appendChild(chip);
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

  /* ── 模型/服务/高级组卡片 ── */
  function groupCard(g) {
    const card = el('div', 'card perm-card');
    const head = el('header');
    const titleWrap = el('div', 'card-title');
    titleWrap.appendChild(el('span', 'doc-title-main', g.label || g.key));
    if (g.hint) titleWrap.appendChild(infoTip(g.hint));
    if (g.refresh === 'restart') {
      titleWrap.appendChild(el('span', 'badge badge-warning', '需重启'));
    }
    head.appendChild(titleWrap);

    const scope = ConfigData.groupScope(g);
    const path = ConfigData.groupPath(g);
    const needTest = g.saveGate === 'test';
    const actions = el('div', 'card-actions');
    let tested = false;         // 服务分组：本组当前表单值是否已测通

    /* 保存只提交本卡片所在的这一个分组 */
    const buildPayload = () => {
      if (scope === 'model') {
        const modelRow = (g.configParams || []).find(p => p.key === 'model');
        const modelName = modelRow ? String(modelRow.value || '').trim() : '';
        const ep = String(g.endpoint || '').trim();
        if (ep && !modelName) {
          const err = new Error((g.label || g.key) + '：已填 API 地址，模型名称也必填');
          err.validation = true;
          throw err;
        }
        if (!ep && modelName) {
          const err = new Error((g.label || g.key) + '：已填模型名称，API 地址也必填');
          err.validation = true;
          throw err;
        }
      }
      return ConfigData.groupPayload(g);
    };
    const save = moduleSaveControls(path, buildPayload,
                                    () => !needTest || tested, g.label || g.key);

    /* 字段被改动 → 上次那次"测通"不再为当前值背书，保存重新变灰 */
    const touch = () => {
      if (needTest && tested) tested = false;
      ConfigData.markDirty();
      save.refresh();
    };

    let modelInput = null;   // 「模型名称」输入框引用，chips 选用时同步
    if (g.showTestButton) {
      const t = el('button', 'btn btn-ghost btn-sm', '测试连接');
      t.addEventListener('click', async () => {
        t.disabled = true;
        t.textContent = '测试中…';
        try {
          const apiKeyRow = (g.configParams || []).find(p => p.key === 'api_key');
          const modelRow = (g.configParams || []).find(p => p.key === 'model');
          const r = await ConfigData.testConnection(
            g.testKind || g.key, g.endpoint || '',
            apiKeyRow ? String(apiKeyRow.value || '') : '',
            modelRow ? String(modelRow.value || '') : '',
            g.configParams || []);
          t.textContent = r.online ? `✓ ${r.latencyMs}ms` : '✗ 失败';
          if (r.online) {
            // 通过：只说"连接正常"，成功提示自动消失
            showToast(r.message || '连接正常', 'success');
            tested = true;
          } else {
            // 不通过：给出探测到的具体原因，弹窗常驻，点 ✕ 才消失
            showToast(r.message || '连接失败', 'error', 0);
            tested = false;
          }
          save.refresh();
          if (Array.isArray(r.models) && r.models.length) {
            attachModelChips(card, r.models,
              () => (modelInput ? modelInput.value : ''),
              name => {
                if (modelInput) {
                  modelInput.value = name;
                  modelInput.dispatchEvent(new Event('input'));
                }
              });
          } else if (Array.isArray(r.models) && r.online && g.endpoint !== undefined) {
            // 只对模型服务提示"可手填模型名"；数据库/中间件本来就没有模型列表
            showToast('端点未返回模型列表，可手填模型名', 'info');
          }
        } catch (e) {
          t.textContent = '✗ 失败';
          showToast('测试失败：' + e.message, 'error', 0);
          tested = false;
          save.refresh();
        } finally {
          setTimeout(() => { t.disabled = false; t.textContent = '测试连接'; }, 3000);
        }
      });
      actions.appendChild(t);
    }
    actions.appendChild(save.tag);
    actions.appendChild(save.btn);
    head.appendChild(actions);
    card.appendChild(head);

    const rows = el('div', 'perm-rows');
    if (g.endpoint !== undefined) {
      rows.appendChild(kvRow('API 地址', textInput(g.endpoint, v => {
        g.endpoint = v; touch();
      }, { placeholder: 'http://host:port/v1（留空则降级为内置 Mock）' }), true));
    }
    // configParams（扁平键值，按类型渲染输入框）
    (g.configParams || []).forEach(p => {
      // bool 型一律给单选，不让人手填 true/false：
      // 后端按字符串判真值（"1 / true / yes / on" 之外都算 false），填错只会静默变 false
      if (p.type === 'bool') {
        const pair = BOOL_OPTION_LABELS[p.key] || BOOL_OPTION_FALLBACK;
        const on = String(p.value).toLowerCase() === 'true';
        rows.appendChild(radioRow(p.label || p.key,
          'svc-bool-' + g.key + '-' + p.key,
          [{ v: 'true', label: pair[0] }, { v: 'false', label: pair[1] }],
          on ? 'true' : 'false',
          v => { p.value = v; touch(); }));
        // 字段级说明另起一行：.perm-row 是横向 flex，塞进去会挤在单选右侧
        if (BOOL_FIELD_HINTS[p.key]) {
          const hint = el('div', 'form-hint', BOOL_FIELD_HINTS[p.key]);
          hint.style.marginLeft = '138px';   // 对齐输入列（label 固定 130px + gap 8px）
          rows.appendChild(hint);
        }
        return;
      }
      const opts = {};
      if (p.type === 'int' || p.type === 'float') {
        opts.type = 'number';
        if (p.type === 'float') opts.placeholder = '小数';
      }
      if (p.secret) opts.type = 'password';
      if (p.key === 'model') opts.placeholder = '如 qwen2.5-14b-instruct / bge-m3';
      const input = textInput(p.value, v => {
        p.value = v; touch();
      }, opts);
      rows.appendChild(kvRow(p.label || p.key, input, p.key === 'model', p.optional));
      if (p.key === 'model') modelInput = input;
    });
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
          showToast('JSON 格式错误', 'error');
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

  /* ── 重排模型块（模型 tab 第三块：本地 Cross-Encoder） ── */
  function renderRerankBlock() {
    const c = ConfigData.state.config;
    const r = (c && c.retrieval) || {};
    const card = el('div', 'card perm-card');
    const head = el('header');
    const titleWrap = el('div', 'card-title');
    titleWrap.appendChild(el('span', 'doc-title-main', '重排模型 (Rerank)'));
    if (r.rerankEnabled === false) {
      titleWrap.appendChild(el('span', 'badge badge-warning', '未启用'));
    }
    head.appendChild(titleWrap);

    // 本块只负责 retrieval 下的两个键，保存时也只提交这两个键
    const paths = ['retrieval.rerankModel', 'retrieval.rerankDevice'];
    const save = moduleSaveControls(paths, () => ({
      retrieval: { rerankModel: r.rerankModel || '', rerankDevice: r.rerankDevice || 'cpu' },
    }));
    const touch = () => { ConfigData.markDirty(); save.refresh(); };

    const actions = el('div', 'card-actions');
    const t = el('button', 'btn btn-ghost btn-sm', '测试连接');
    t.addEventListener('click', async () => {
      t.disabled = true;
      t.textContent = '测试中…';
      try {
        const res = await ConfigData.testConnection('rerank', '', '', r.rerankModel || '');
        t.textContent = res.online ? `✓ ${res.latencyMs}ms` : '✗ 失败';
        showToast(res.message || (res.online ? '模型可用' : '验证失败'),
                  res.online ? 'success' : 'error', res.online ? undefined : 0);
        if (Array.isArray(res.models) && res.models.length) {
          attachModelChips(card, res.models,
            () => modelInput.value,
            name => {
              modelInput.value = name;
              modelInput.dispatchEvent(new Event('input'));
            });
        }
      } catch (e) {
        t.textContent = '✗ 失败';
        showToast('测试失败：' + e.message, 'error', 0);
      } finally {
        setTimeout(() => { t.disabled = false; t.textContent = '测试连接'; }, 3000);
      }
    });
    actions.appendChild(t);
    actions.appendChild(save.tag);
    actions.appendChild(save.btn);
    head.appendChild(actions);
    card.appendChild(head);

    const rows = el('div', 'perm-rows');
    const modelInput = textInput(r.rerankModel || '', v => {
      r.rerankModel = v; touch();
    }, { placeholder: 'models/ 下的权重目录名，或 HuggingFace ID（如 BAAI/bge-reranker-v2-m3）' });
    rows.appendChild(kvRow('模型名称', modelInput, false));
    rows.appendChild(radioRow('推理设备', 'rerank-device',
      [{ v: 'cpu', label: 'CPU' }, { v: 'cuda', label: 'GPU (CUDA)' }],
      r.rerankDevice || 'cpu',
      v => { r.rerankDevice = v; touch(); }));
    card.appendChild(rows);
    return card;
  }

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
    }));
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
      // 模型 tab：从上到下三个块（LLM / Embedding / Rerank），底部为检索策略
      const wrap = el('div');
      const stack = el('div', 'model-stack');
      (data.groups || []).forEach(g => stack.appendChild(groupCard(g)));
      stack.appendChild(renderRerankBlock());
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
