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

  /* 某个模块倒回上次落盘的那一套（「放弃修改」）：从基线里把这一块整体取回来。
     基线就是"这个模块已保存/已认可"的值 —— markSaved() 在入库、切换选中、激活等
     操作后都会把它同步过去，所以取基线即取用户上次认可的那套配置（模型卡片上是
     "当前生效的配置"或"点开的那条库条目"）。
     只动本模块，别的模块的未保存改动不受牵连（与保存同一粒度）。 */
  function restoreModule(paths) {
    let base;
    try {
      base = JSON.parse(state.baseline);
    } catch (e) { return false; }
    (Array.isArray(paths) ? paths : [paths])
      .forEach(p => _patch(state.config, base, p));
    state.dirty = JSON.stringify(state.config) !== state.baseline;
    return true;
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
      // 模型段不再走"整段覆盖"保存（见 upsertModel）：后端也会拒收这类载荷，
      // 在这里就挡住，免得走到接口才失败
      throw new Error('模型配置走模型库接口保存（ConfigData.upsertModel）');
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

  /* ── 模型库（模型 tab）──
     库 = 这一段里所有「已测通」的配置；active 的那一条就是运行参数（后端
     models._sync_model_library 会把顶层字段按它重写）。三个操作都直接落盘并热
     应用，返回该段最新的库视图与 active 条目 —— 前端据此同时刷新左列表与右表单。 */
  function _applyLibrary(section, res) {
    const g = (state.config.modelProviders || []).find(x => x.key === section);
    if (!g) return null;
    g.library = res.library;
    // 右侧表单显示的就是"当前生效的配置"：只有 agent 条目变了它才该变
    if (res.active) {
      g.endpoint = res.active.endpoint;
      // 只取第一个：它是这条配置调用的那个，也是表单「模型ID」框该显示的值
      g.modelIds = (res.active.modelIds || []).slice(0, 1);
      g.configParams = JSON.parse(JSON.stringify(res.active.configParams || []));
    }
    markSaved(['model:' + section]);   // 服务端已落盘：同步本模块基线，别亮"未保存"
    return g;
  }

  /* 存入模型库：body = {id, endpoint, configParams, verified, activate}
     id 为空 = 新增，非空 = 更新那一条 */
  async function upsertModel(section, body) {
    const res = await API.post('/api/admin/model-library/upsert',
      Object.assign({ section: section }, body));
    return { res: res, group: _applyLibrary(section, res) };
  }

  async function activateModel(section, id) {
    const res = await API.post('/api/admin/model-library/activate',
      { section: section, id: id });
    return { res: res, group: _applyLibrary(section, res) };
  }

  async function deleteModel(section, id) {
    const res = await API.post('/api/admin/model-library/delete',
      { section: section, id: id });
    return { res: res, group: _applyLibrary(section, res) };
  }

  /* params：所在分组的 configParams 行（含 type），后端据此还原类型直连探测，
     使「改了表单但还没保存」也能测到真实值，而不是已保存的旧配置。
     模型卡片走的是"真调用"：后端拿地址 + Key + 模型ID 发一次最小请求，
     有正确回应才算 r.online=true。
     entryId：正在编辑的那条库条目 —— 框里的圆点没动时后端用它**这一条自己的**
     钥匙，而不是 active 那条借来的（见 routes._effective_api_key）
     apiKeyCleared：用户把框里那串圆点删干净了 = 主动清空，后端就按空钥匙测 */
  async function testConnection(kind, endpoint, apiKey, model, params, entryId,
                                apiKeyCleared) {
    return API.post('/api/admin/health/test',
      { kind, endpoint, apiKey: apiKey || '', model: model || '',
        params: params || [], entryId: entryId || '',
        apiKeyCleared: !!apiKeyCleared });
  }

  /* 「获取模型ID」：按表单里的地址 + Key 拉一次 /models，只返回候选列表，
     不做可用性判定（那是 testConnection 的事）。列表为空也算成功 ——
     有的服务不实现 /models，用户手填模型ID即可。Key 的回落规则同上 */
  async function listModels(kind, endpoint, apiKey, params, entryId, apiKeyCleared) {
    return API.post('/api/admin/model-library/list-models',
      { kind, endpoint, apiKey: apiKey || '', params: params || [],
        entryId: entryId || '', apiKeyCleared: !!apiKeyCleared });
  }

  window.ConfigData = {
    state, load, markDirty, clearDirty, groupsFor,
    moduleDirty, markSaved, restoreModule, saveModule, groupScope, groupPath,
    groupPayload,
    testConnection, listModels, upsertModel, activateModel, deleteModel,
  };
})();
