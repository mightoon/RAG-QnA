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

  const KNOWN_STATUS = ['queued', 'parsing', 'embedding', 'uploading', 'indexing',
    'done', 'failed', 'partial', 'deleted', 'superseded', 'retrying'];

  const state = {
    tab: 'docs',
    docs: { page: 1, size: 10, status: '', collection: '', q: '', archivedOnly: false },
    tasks: { page: 1, size: 10, status: '', collection: '' },
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
    if (tab === 'trash' && !state.trashLoaded) initTrashTab();
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

  function refreshAll() {
    const pieces = ['stats_cards'];
    if (state.tab === 'docs') pieces.push('docs_tab');
    if (state.tab === 'tasks') pieces.push('tasks_tab');
    if (state.tab === 'trash') pieces.push('trash_tab');
    return Partials.invalidateAndRefresh(pieces, {
      query: Object.assign(docsQuery(), tasksQuery()),
    });
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
      showToast('已重新入队', 'success');
      refreshAll();
    } catch (e) { showToast('重建失败：' + e.message, 'error'); }
  }

  function downloadDoc(docId, filename) {
    const a = document.createElement('a');
    a.href = '/api/documents/' + encodeURIComponent(docId) + '/content';
    a.download = filename || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function deleteDoc(docId, filename) {
    const ok = await confirmDialog({
      title: '删除文档',
      message: `文档「${filename}」将移入回收站，可随时恢复。`,
      detail: '删除后该文档不再参与检索。',
      confirmText: '移入回收站',
    });
    if (!ok) return;
    try {
      await API.delete('/api/documents/' + encodeURIComponent(docId));
      showToast('已移入回收站', 'success');
      refreshAll();
    } catch (e) { showToast('删除失败：' + e.message, 'error'); }
  }

  async function restoreDoc(docId) {
    try {
      await API.post('/api/documents/' + encodeURIComponent(docId) + '/restore', {});
      showToast('已恢复', 'success');
      refreshAll();
    } catch (e) { showToast('恢复失败：' + e.message, 'error'); }
  }

  async function permanentDelete(docId, filename) {
    const ok = await confirmDialog({
      title: '永久删除文档',
      message: `将永久删除「${filename}」及其全部索引数据，此操作不可恢复。`,
      detail: '将清理向量库 / 全文索引 / 图谱 / 对象存储中的关联数据。',
      type: 'danger', confirmWord: 'DELETE',
    });
    if (!ok) return;
    try {
      await API.delete('/api/documents/' + encodeURIComponent(docId) + '/permanent');
      showToast('已永久删除', 'success');
      refreshAll();
    } catch (e) { showToast('删除失败：' + e.message, 'error'); }
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
    bindDocActions(document);
    return Partials.refreshPiece('trash_tab', { query: docsQuery() });
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
  Partials.registerRebinder('trash_tab', bindDocEvents);
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
    switchTab(state.tab, { pushUrl: false });
    if (window.KnowledgeUpload) KnowledgeUpload.init({ refreshAll, state });
    if (window.KnowledgeTasks) KnowledgeTasks.init({ refreshAll, state, updateTaskBadges });
  }

  window.Knowledge = { state, switchTab, refreshAll, reingestDoc, downloadDoc };
  document.addEventListener('DOMContentLoaded', init);
})();
