/* 知识库管理页主控制器
   依赖：state.js / api.js / sse.js / partials.js / knowledge-upload.js / knowledge-tasks.js
   负责：Tab 状态机、列表筛选、文档操作、统计
*/
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const qsa = (s, r) => Array.from((r || document).querySelectorAll(s));
  const showToast = (msg, type) => {
    if (window.Partials && window.Partials.showToast) window.Partials.showToast(msg, type);
    else console.log('[toast]', type, msg);
  };

  const state = {
    tab: 'docs',
    docs: { page: 1, size: 10, status: '', collection: '', q: '', archivedOnly: false },
    tasks: { page: 1, size: 10, status: '', collection: '' },
    /* 回收站勾选：Set of doc_id。刻意放在 state 里而不是只读 DOM ——
       列表片段会被局部刷新整块替换（在看任务页时后台跑着入库也会触发刷新），
       DOM 一换勾选就没了，用户会以为"勾好的又被取消了"。 */
    trash: { sel: new Set() },
    focusDocId: '',
    focusHandled: false,
    queue: { queuedTasks: 0, queueDepthLimit: null, queueFull: false },
  };

  /* ── Tab 状态机 ── */
  function switchTab(tab, opts) {
    opts = opts || {};
    if (!['docs', 'tasks', 'trash'].includes(tab)) tab = 'docs';
    state.tab = tab;
    qsa('[data-tab]', qs('.knowledge-tabs')).forEach(b => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });
    qsa('[data-tab-panel]').forEach(p => {
      p.classList.toggle('hidden', p.dataset.tabPanel !== tab);
    });
    if (opts.pushUrl !== false) {
      const url = new URL(location.href);
      url.searchParams.set('tab', tab);
      history.replaceState(null, '', url);
    }
    if (tab === 'docs') state.docs.archivedOnly = false;
    if (tab === 'docs' && !state.docs.loaded) initDocsTab();
    if (tab === 'tasks' && !state.tasks.loaded) initTasksTab();
    if (tab === 'trash') {
      // 每次切回来都重拉一次：回收站里的内容会被"别处"改动（详情页删除/恢复、
      // 入库收尾），列表停在上次渲染的状态会显示已经不存在的行。
      // 勾选状态存在 state.trash.sel 里，刷新不会丢。
      if (state.trashLoaded) Partials.refreshPiece('trash_tab', { query: trashQuery() });
      else initTrashTab();
    }
  }

  /* ── 查询参数（供各 piece 刷新使用）── */
  function docsQuery() {
    const d = state.docs;
    return {
      collection: d.collection, status: d.archivedOnly ? 'deleted' : d.status,
      q: d.q, page: d.page, size: d.size, archivedOnly: d.archivedOnly ? 1 : '',
    };
  }
  function tasksQuery() {
    const t = state.tasks;
    return { collection: t.collection, status: t.status, page: t.page, size: t.size };
  }
  /* 回收站**不带**文档页的筛选条件：回收站面板里没有集合/状态/搜索输入框，
     若沿用文档页的筛选，"回收站里少了几篇"会变成一个看不见的过滤器造成的谜题。
     分页沿用文档页的页码与页长（两个列表共用一套分页控件）。 */
  function trashQuery() {
    return { collection: '', status: '', q: '', page: state.docs.page,
             size: state.docs.size, archivedOnly: 1 };
  }

  function refreshAll() {
    const pieces = ['stats_cards'];
    if (state.tab === 'docs') pieces.push('docs_tab');
    if (state.tab === 'tasks') pieces.push('tasks_tab');
    if (state.tab === 'trash') pieces.push('trash_tab');
    const query = state.tab === 'trash'
      ? Object.assign(trashQuery(), tasksQuery())
      : Object.assign(docsQuery(), tasksQuery());
    return Partials.invalidateAndRefresh(pieces, { query });
  }

  /* ── 统计 ── */
  function initStats() {
    return Partials.refreshPiece('stats_cards', { query: {} });
  }

  /* ── 文档列表 ── */
  function initDocsTab() {
    state.docs.loaded = true;
    bindDocEvents(document);
    return Partials.refreshPiece('docs_tab', { query: docsQuery() });
  }

  function bindDocEvents(root) {
    const sel = qs('#knowledge-page .collection-select', root);
    if (sel && !sel.dataset.bound) {
      sel.dataset.bound = '1';
      sel.addEventListener('change', () => {
        state.docs.collection = sel.value;
        state.docs.page = 1;
        Partials.refreshPiece('docs_tab', { query: docsQuery() });
      });
    }
    const ss = qs('#doc-status-filter', root);
    if (ss && !ss.dataset.bound) {
      ss.dataset.bound = '1';
      ss.addEventListener('change', () => {
        state.docs.status = ss.value;
        state.docs.page = 1;
        Partials.refreshPiece('docs_tab', { query: docsQuery() });
      });
    }
    const search = qs('#doc-search', root);
    if (search && !search.dataset.bound) {
      search.dataset.bound = '1';
      let timer = null;
      search.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          state.docs.q = search.value.trim();
          state.docs.page = 1;
          Partials.refreshPiece('docs_tab', { query: docsQuery() });
        }, 350);
      });
    }
    qsa('.js-doc-page', root).forEach(btn => {
      btn.addEventListener('click', () => {
        state.docs.page = parseInt(btn.dataset.page, 10) || 1;
        Partials.refreshPiece('docs_tab', { query: docsQuery() });
      });
    });
    bindDocActions(root);
  }

  function bindDocActions(root) {
    qsa('[data-doc-action]', root).forEach(btn => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', () => {
        const docId = btn.dataset.docId;
        const filename = btn.dataset.filename || docId;
        const action = btn.dataset.docAction;
        if (action === 'reingest') reingestDoc(docId);
        else if (action === 'download') downloadDoc(docId, filename);
        else if (action === 'delete') deleteDoc(docId, filename);
        else if (action === 'restore') restoreDoc(docId);
        else if (action === 'permanent') permanentDelete(docId, filename);
        else if (action === 'open') {
          location.href = '/knowledge/docs/' + encodeURIComponent(docId);
        }
      });
    });
  }

  async function reingestDoc(docId) {
    try {
      await API.post('/api/ingest/documents/' + encodeURIComponent(docId) + '/reingest', {});
      showToast('已开始重解析（沿用原文档，按当前解析口径重跑，稍后刷新看进度）', 'success');
      refreshAll();
    } catch (e) { showToast('重解析失败：' + e.message, 'error'); }
  }

  /* 下载**原始文件**：走 /api/documents/{id}/download（服务端从对象存储取回，
     带 attachment 头），并用 API.download 带上 Token —— 见 api.js 里的说明。
     注意不要用 {raw:true}：request 在非 2xx 时会抛 ApiError，
     这里只关心"成没成、失败原因是什么"。 */
  async function downloadDoc(docId, filename) {
    try {
      await API.download('/api/documents/' + encodeURIComponent(docId) + '/download',
                         filename);
      showToast('已开始下载：' + (filename || ''), 'success');
    } catch (e) {
      showToast('下载失败：' + e.message, 'error');
    }
  }

  /* 删除 = **移入回收站**（用户要求的两段式）：
     一个动作只把文档从列表里拿掉，块/向量/全文索引/图谱/对象存储里的原文件
     一概不动 —— 所以「恢复」是原样的，不用重新解析。
     真正清库的是回收站里的「彻底删除」（勾选后触发，见 purgeSelected）。
     正在入库/重解析的文档后端会拒（409）：那一篇收尾时会把状态写回 done，
     删了也会自己回来，不如当场说清楚。 */
  async function deleteDoc(docId, filename) {
    const ok = await confirmDialog({
      title: '移入回收站',
      message: `将「${filename}」移入回收站，列表里不再显示。`,
      detail: '回收站中的文档不参与检索，内容仍在各数据库中保存；'
            + '可以随时「恢复」放回列表。要彻底清除请到回收站勾选后点「彻底删除」。',
      confirmText: '移入回收站',
    });
    if (!ok) return;
    try {
      await API.delete('/api/documents/' + encodeURIComponent(docId));
      showToast('已移入回收站（可在回收站恢复或彻底删除）', 'success');
      refreshAll();
    } catch (e) { showToast('删除失败：' + e.message, 'error'); }
  }

  async function restoreDoc(docId) {
    try {
      const r = await API.post('/api/documents/' + encodeURIComponent(docId) + '/restore', {});
      showToast('已恢复到列表（状态：' + ((r && r.restoredTo) || 'done') + '）', 'success');
      refreshAll();
    } catch (e) { showToast('恢复失败：' + e.message, 'error'); }
  }

  /* 回收站单篇彻底删除：与批量走同一个接口，只差一个数组长度 */
  async function permanentDelete(docId, filename) {
    const ok = await confirmDialog({
      title: '彻底删除文档',
      message: `将彻底删除「${filename}」及其在所有数据库中的内容，此操作不可恢复。`,
      detail: '会一并清理：向量库（Milvus）、全文索引（Elasticsearch）、知识图谱、'
            + '对象存储里的原始文件（MinIO）、以及元数据里的文档/分块/表格数据。',
      type: 'danger', confirmText: '彻底删除', confirmWord: 'DELETE',
    });
    if (!ok) return;
    await purgeDocs([docId]);
  }

  /* ── 回收站：勾选 + 批量恢复 / 批量彻底删除 ── */
  function selectedIds() { return Array.from(state.trash.sel); }

  function updateTrashSel() {
    const n = state.trash.sel.size;
    const cnt = qs('#trash-sel-count');
    if (cnt) cnt.innerHTML = '已选 <b>' + n + '</b> 项';
    const rb = qs('#btn-trash-restore');
    const pb = qs('#btn-trash-purge');
    if (rb) rb.disabled = n === 0;
    if (pb) pb.disabled = n === 0;
    const all = qs('#trash-select-all');
    if (all) {
      const boxes = qsa('.js-trash-check');
      const checked = boxes.filter(b => b.checked).length;
      all.checked = boxes.length > 0 && checked === boxes.length;
      all.indeterminate = checked > 0 && checked < boxes.length;
    }
  }

  /* 片段刷新后重新套用勾选：刷新可能发生在用户勾选之后（后台任务推送），
     勾选状态存在 state 里，这里把它盖回新渲染出来的复选框上。 */
  function bindTrashEvents(root) {
    qsa('.js-trash-check', root).forEach(box => {
      if (box.dataset.bound) return;
      box.dataset.bound = '1';
      if (state.trash.sel.has(box.value)) box.checked = true;
      box.addEventListener('change', () => {
        if (box.checked) state.trash.sel.add(box.value);
        else state.trash.sel.delete(box.value);
        updateTrashSel();
      });
    });
    const all = qs('#trash-select-all', root);
    if (all && !all.dataset.bound) {
      all.dataset.bound = '1';
      all.addEventListener('change', () => {
        qsa('.js-trash-check', root).forEach(box => {
          box.checked = all.checked;
          if (all.checked) state.trash.sel.add(box.value);
          else state.trash.sel.delete(box.value);
        });
        updateTrashSel();
      });
    }
    // 已不在列表里的 id（被删掉/换了筛选/翻了页）从勾选集合里剔除：
    // 留着它会让"已选 N 项"和实际可见的行对不上，批量删除也就删了看不到的东西。
    const present = new Set(qsa('.js-trash-check', document).map(b => b.value));
    Array.from(state.trash.sel).forEach(id => {
      if (!present.has(id)) state.trash.sel.delete(id);
    });
    updateTrashSel();
  }

  async function restoreSelected() {
    const ids = selectedIds();
    if (!ids.length) return;
    const ok = await confirmDialog({
      title: '恢复文档',
      message: `将选中的 ${ids.length} 篇文档放回文档列表。`,
      detail: '状态还原成删除前的状态，内容不重新解析。',
      confirmText: '恢复',
    });
    if (!ok) return;
    try {
      const r = await API.post('/api/documents/restore', { doc_ids: ids });
      const bad = (r && r.failed) || [];
      state.trash.sel.clear();
      updateTrashSel();      // 立刻把计数/按钮复位：不等列表刷新回来（刷新是异步的，期间还能再点）
      if (bad.length) {
        showToast(`已恢复 ${(r.restored || []).length} 篇；${bad.length} 篇失败：`
                  + bad.map(f => f.error).join('、'), 'warning');
      } else {
        showToast(`已恢复 ${(r.restored || []).length} 篇文档`, 'success');
      }
      refreshAll();
    } catch (e) { showToast('恢复失败：' + e.message, 'error'); }
  }

  /* 彻底删除：**只有在这里**才会落到五个数据库上。
     弹框把要删的文件名列出来（最多 5 个 + 其余计数），并要求输入 DELETE ——
     批量清库不可撤销，值得多一次确认。 */
  function purgeConfirmMessage(ids) {
    const names = ids.slice(0, 5).map(id => {
      const tr = document.querySelector('tr[data-doc-id="' + id + '"] .cell-name');
      return (tr && tr.textContent.trim()) || id;
    });
    const rest = ids.length - names.length;
    const list = names.join('、') + (rest > 0 ? ` 等 ${ids.length} 篇` : '');
    return { message: `将彻底删除 ${ids.length} 篇文档：${list}`,
             detail: '清除向量库（Milvus）、全文索引（Elasticsearch）、知识图谱、'
                   + '对象存储里的原始文件（MinIO）与元数据（文档/分块/表格行）。'
                   + '此操作不可恢复。' };
  }

  async function purgeDocs(ids) {
    if (!ids || !ids.length) return;
    try {
      const r = await API.post('/api/documents/purge', { doc_ids: ids });
      const failed = (r && r.failed) || [];
      const skipped = (r && r.skipped) || [];
      const warns = (r && r.warnings) || [];
      const done = (r && r.deleted) || [];
      ids.forEach(id => state.trash.sel.delete(id));
      updateTrashSel();      // 立刻复位计数/按钮：刷新是异步的，期间不该还能再点一次
      if (failed.length) {
        showToast(`部分清理失败：` + failed.map(f => f.store).join('、')
                  + `（已删 ${done.length} 篇，其余可再试一次）`, 'warning');
      } else if (skipped.length && !done.length) {
        // 一篇都没删（都被闸门挡下）时不要报"已彻底删除 0 篇"——那句话自相矛盾
        showToast(`${skipped.length} 篇跳过：` + skipped.map(s => s.reason).join('、'),
                  'warning');
      } else if (skipped.length) {
        showToast(`已彻底删除 ${done.length} 篇；${skipped.length} 篇跳过：`
                  + skipped.map(s => s.reason).join('、'), 'warning');
      } else {
        showToast(`已彻底删除 ${done.length} 篇（五库内容均已清理）`, 'success');
      }
      if (warns.length) {
        showToast(`${warns.length} 篇的原始文件与其它文档行共用，已保留下载原件`,
                  'warning');
      }
      refreshAll();
    } catch (e) { showToast('彻底删除失败：' + e.message, 'error'); }
  }

  async function purgeSelected() {
    const ids = selectedIds();
    if (!ids.length) return;
    const { message, detail } = purgeConfirmMessage(ids);
    const ok = await confirmDialog({
      title: '彻底删除选中文档', message, detail,
      type: 'danger', confirmText: '彻底删除', confirmWord: 'DELETE',
    });
    if (!ok) return;
    await purgeDocs(ids);
  }

  /* ── 任务列表 ── */
  function initTasksTab() {
    state.tasks.loaded = true;
    bindTaskEvents(document);
    return Partials.refreshPiece('tasks_tab', { query: tasksQuery() });
  }

  function bindTaskEvents(root) {
    const ss = qs('#task-status-filter', root);
    if (ss && !ss.dataset.bound) {
      ss.dataset.bound = '1';
      ss.addEventListener('change', () => {
        state.tasks.status = ss.value;
        state.tasks.page = 1;
        Partials.refreshPiece('tasks_tab', { query: tasksQuery() });
      });
    }
    const cs = qs('#task-collection-filter', root);
    if (cs && !cs.dataset.bound) {
      cs.dataset.bound = '1';
      cs.addEventListener('change', () => {
        state.tasks.collection = cs.value;
        state.tasks.page = 1;
        Partials.refreshPiece('tasks_tab', { query: tasksQuery() });
      });
    }
    qsa('.js-task-page', root).forEach(btn => {
      btn.addEventListener('click', () => {
        state.tasks.page = parseInt(btn.dataset.page, 10) || 1;
        Partials.refreshPiece('tasks_tab', { query: tasksQuery() });
      });
    });
    bindTaskActions(root);
  }

  function bindTaskActions(root) {
    qsa('[data-task-action="retry"]', root).forEach(btn => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', async () => {
        const taskId = btn.dataset.taskId;
        btn.disabled = true;
        try {
          const r = await API.post('/api/tasks/' + encodeURIComponent(taskId) + '/retry', {});
          if (r.ok === false) showToast(r.error || '不支持重试', 'warning');
          else showToast('已重试', 'success');
          refreshAll();
        } catch (e) { showToast('重试失败：' + e.message, 'error'); }
        finally { btn.disabled = false; }
      });
    });
  }

  /* ── 回收站 ── */
  function initTrashTab() {
    state.trashLoaded = true;
    state.docs.archivedOnly = true;
    state.docs.status = '';
    state.docs.page = 1;
    bindDocEvents(document);
    bindTrashEvents(document.querySelector('[data-piece="trash_tab"]') || document);
    return Partials.refreshPiece('trash_tab', { query: trashQuery() });
  }

  /* 工具栏按钮（在 knowledge.jinja2 里，片段刷新不会替换它们）→ 只绑一次 */
  function bindTrashToolbar() {
    const rb = qs('#btn-trash-restore');
    const pb = qs('#btn-trash-purge');
    if (rb) rb.addEventListener('click', restoreSelected);
    if (pb) pb.addEventListener('click', purgeSelected);
    updateTrashSel();
  }

  function updateTaskBadges(activeCount) {
    const b = qs('#active-tasks-badge');
    if (b) {
      b.textContent = activeCount;
      b.classList.toggle('hidden', !activeCount);
    }
  }

  /* ── piece 重绑定注册 ── */
  Partials.registerRebinder('docs_tab', bindDocEvents);
  /* 回收站：文档列表的通用事件（分页/操作按钮）+ 勾选状态套用 */
  Partials.registerRebinder('trash_tab', root => {
    bindDocEvents(root);
    bindTrashEvents(root);
  });
  Partials.registerRebinder('tasks_tab', bindTaskEvents);

  /* ── 初始化 ── */
  function init() {
    const el = qs('#knowledge-page');
    if (!el) return;
    state.tab = el.dataset.activeTab || 'docs';
    state.focusDocId = el.dataset.focusDocId || '';
    try { state.queue = JSON.parse(el.dataset.queue || '{}') || state.queue; }
    catch (e) { /* ignore */ }

    qsa('[data-tab]', qs('.knowledge-tabs')).forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    initStats();
    bindTrashToolbar();
    switchTab(state.tab, { pushUrl: false });
    if (window.KnowledgeUpload) KnowledgeUpload.init({ refreshAll, state });
    if (window.KnowledgeTasks) KnowledgeTasks.init({ refreshAll, state, updateTaskBadges });
  }

  /* 回收站相关的函数一并导出：页面内的调试与自测要用
     （tmp_selftest/t_trash_ui.mjs 直接驱动"勾选 → 彻底删除"这条链路） */
  window.Knowledge = {
    state, switchTab, refreshAll, reingestDoc, downloadDoc, restoreDoc,
    purgeDocs, purgeSelected, restoreSelected, bindTrashEvents, updateTrashSel,
  };
  document.addEventListener('DOMContentLoaded', init);
})();
