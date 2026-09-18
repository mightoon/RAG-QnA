/* 配置数据层：统一载荷缓存、按模块的脏跟踪与保存
   设计要点：保存一律以「模块」为最小单位——点某个卡片的保存只提交该模块的键，
   后端收到缺省的域不会写它们，因此不会顺带把别的模块的未保存改动一起落盘。 */
(function () {
  'use strict';

  const state = {
    config: null,        // 服务端统一载荷（深拷贝后可编辑）
    baseline: '',        // JSON 基线：仅用于判断某个模块是否已落盘
    dirty: false,        // 是否存在未落盘的改动（离开页面提醒用）
  };

  /* 卡片 tab → 统一载荷中的数组字段名 */
  const GROUP_PATH = {
    model: 'modelProviders', service: 'serviceGroups', special: 'specialGroups',
  };

  function load() {
    const init = window.__CONFIG_INIT__;
    state.config = init ? JSON.parse(JSON.stringify(init)) : null;
    state.baseline = JSON.stringify(state.config);
    state.dirty = false;
    return state.config;
  }

  function markDirty() {
    // 只置全局标记（beforeunload 拦截）；是否可保存由 moduleDirty 按基线判断
    state.dirty = true;
  }

  function clearDirty() {
    state.dirty = false;
    state.baseline = JSON.stringify(state.config);
  }

  function groupsFor(tab) {
    const c = state.config;
    if (!c) return [];
    if (tab === 'model') return c.modelProviders || [];
    if (tab === 'service') return c.serviceGroups || [];
    if (tab === 'special') return c.specialGroups || [];
    return [];
  }

  /* ── 模块路径定位 ──
     'model:llm'           → modelProviders 里 key=llm 的那一项（数组分组）
     'retrieval.defaultWeights' → retrieval 对象的某个字段
     'permissionMappings'  → 顶层整块
     返回 {obj, key}，便于统一读写同一位置 */
  function _locate(root, path) {
    if (!root) return null;
    const gi = path.indexOf(':');
    if (gi > 0) {
      const arrName = GROUP_PATH[path.slice(0, gi)] || path.slice(0, gi);
      const arr = root[arrName];
      if (!Array.isArray(arr)) return null;
      const i = arr.findIndex(x => x && x.key === path.slice(gi + 1));
      return i < 0 ? null : { obj: arr, key: i };
    }
    const di = path.indexOf('.');
    if (di > 0) {
      const head = root[path.slice(0, di)];
      return head == null ? null : { obj: head, key: path.slice(di + 1) };
    }
    return { obj: root, key: path };
  }

  /* 把 src 中该模块的当前值覆盖到 dst 的同一位置 */
  function _patch(dstRoot, srcRoot, path) {
    const d = _locate(dstRoot, path), s = _locate(srcRoot, path);
    if (!d || !s) return;
    d.obj[d.key] = JSON.parse(JSON.stringify(s.obj[s.key]));
  }

  /* 该模块当前值是否与上次落盘值不同 */
  function _differs(curRoot, baseRoot, path) {
    const a = _locate(curRoot, path), b = _locate(baseRoot, path);
    if (!a || !b) return false;
    try {
      return JSON.stringify(a.obj[a.key]) !== JSON.stringify(b.obj[b.key]);
    } catch (e) { return true; }
  }

  /* 某个模块是否还有未落盘的改动（决定该模块的保存按钮是否可点） */
  function moduleDirty(paths) {
    let base;
    try {
      base = JSON.parse(state.baseline);
    } catch (e) { return true; }
    return (Array.isArray(paths) ? paths : [paths])
      .some(p => _differs(state.config, base, p));
  }

  /* 某模块已落盘：只把该模块的基线同步到当前值，再按整体差异重算全局标记。
     不能整体 clearDirty()，否则会把别的模块的未保存修改一起"洗白" */
  function markSaved(paths) {
    if (state.baseline) {
      try {
        const base = JSON.parse(state.baseline);
        (Array.isArray(paths) ? paths : [paths]).forEach(p => _patch(base, state.config, p));
        state.baseline = JSON.stringify(base);
      } catch (e) { /* 基线异常：保守不动 */ }
    }
    state.dirty = JSON.stringify(state.config) !== state.baseline;
  }

  /* ── 单模块保存载荷 ── */
  function groupScope(g) {
    return g.saveScope || g.tab || 'special';
  }

  /* 卡片所在模块的路径（用于 dirty 判断与基线同步） */
  function groupPath(g) {
    return groupScope(g) + ':' + g.key;
  }

  /* 只提交这一个分组：其余域缺省 → 后端不会写它们 */
  function groupPayload(g) {
    const scope = groupScope(g);
    if (scope === 'model') {
      return { modelProviders: [{ key: g.key, endpoint: g.endpoint,
                                  paramsJson: g.paramsJson,
                                  configParams: g.configParams || [] }] };
    }
    if (scope === 'service') {
      return { serviceGroups: [{ key: g.key, paramsJson: g.paramsJson || {},
                                 configParams: g.configParams || [] }] };
    }
    return { specialGroups: [{ key: g.key, paramsJson: g.paramsJson || {} }] };
  }

  /* 保存单个模块：payload 只含该模块，paths 用于落盘后同步基线 */
  async function saveModule(payload, paths) {
    const res = await API.post('/api/admin/config', payload);
    markSaved(paths);
    return res;
  }

  /* params：所在分组的 configParams 行（含 type），后端据此还原类型直连探测，
     使「改了表单但还没保存」也能测到真实值，而不是已保存的旧配置 */
  async function testConnection(kind, endpoint, apiKey, model, params) {
    return API.post('/api/admin/health/test',
      { kind, endpoint, apiKey: apiKey || '', model: model || '',
        params: params || [] });
  }

  window.ConfigData = {
    state, load, markDirty, clearDirty, groupsFor,
    moduleDirty, markSaved, saveModule, groupScope, groupPath, groupPayload,
    testConnection,
  };
})();
