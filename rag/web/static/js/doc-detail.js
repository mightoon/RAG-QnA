/* 文档详情页：属性/预览/分块局部刷新 + 文档操作 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const qsa = (s, r) => Array.from((r || document).querySelectorAll(s));
  const showToast = (msg, type) => {
    if (window.Partials && window.Partials.showToast) window.Partials.showToast(msg, type);
    else console.log('[toast]', type, msg);
  };

  let docId = '';
  const chunks = { page: 1, size: 12 };

  function docQuery() { return { docId }; }

  function refreshHeaderAttrs() {
    return Partials.invalidateAndRefresh(['doc_header', 'doc_attrs'], { query: docQuery() });
  }

  /* ── 操作 ── */
  async function doOp(op, filename) {
    const id = encodeURIComponent(docId);
    try {
      if (op === 'reingest') {
        await API.post('/api/ingest/documents/' + id + '/reingest', {});
        showToast('已重新入队', 'success');
        refreshHeaderAttrs();
        Partials.refreshPiece('preview_pane', { query: docQuery() });
      } else if (op === 'download') {
        const a = document.createElement('a');
        a.href = '/api/documents/' + id + '/content';
        a.download = filename || '';
        document.body.appendChild(a);
        a.click();
        a.remove();
      } else if (op === 'delete') {
        const ok = await confirmDialog({
          title: '删除文档', message: `文档「${filename}」将移入回收站。`, confirmText: '移入回收站'
        });
        if (!ok) return;
        await API.delete('/api/documents/' + id);
        showToast('已移入回收站', 'success');
        refreshHeaderAttrs();
      } else if (op === 'restore') {
        await API.post('/api/documents/' + id + '/restore', {});
        showToast('已恢复', 'success');
        refreshHeaderAttrs();
        Partials.refreshPiece('preview_pane', { query: docQuery() });
      } else if (op === 'permanent') {
        const ok = await confirmDialog({
          title: '永久删除文档',
          message: `将永久删除「${filename}」及全部索引数据，不可恢复。`,
          detail: '将清理向量库 / 全文索引 / 图谱 / 对象存储中的关联数据。',
          type: 'danger', confirmWord: 'DELETE'
        });
        if (!ok) return;
        await API.delete('/api/documents/' + id + '/permanent');
        showToast('已永久删除', 'success');
        location.href = '/knowledge?tab=trash';
      }
    } catch (e) { showToast('操作失败：' + e.message, 'error'); }
  }

  /* ── 分块 ── */
  function bindChunks(root) {
    qsa('.js-chunk-page', root).forEach(btn => {
      btn.addEventListener('click', () => {
        chunks.page = parseInt(btn.dataset.page, 10) || 1;
        Partials.refreshPiece('chunks_container', {
          query: Object.assign(docQuery(), { page: chunks.page, size: chunks.size })
        });
      });
    });
    qsa('.chunk-expand-btn', root).forEach(btn => {
      btn.addEventListener('click', () => {
        const item = btn.closest('.chunk-item');
        const expanded = item.classList.toggle('expanded');
        btn.textContent = expanded ? '收起' : '展开';
      });
    });
  }

  /* ── 初始化 ── */
  function init() {
    const page = qs('#doc-detail-page');
    if (!page) return;
    docId = page.dataset.docId;
    const filename = qs('.doc-title-row h1') ? qs('.doc-title-row h1').textContent : docId;

    qsa('[data-doc-op]').forEach(btn => {
      btn.addEventListener('click', () => doOp(btn.dataset.docOp, filename));
    });

    qsa('[data-dtab]').forEach(tab => {
      tab.addEventListener('click', () => {
        qsa('[data-dtab]').forEach(t => t.classList.toggle('active', t === tab));
        qsa('[data-dtab-panel]').forEach(p =>
          p.classList.toggle('hidden', p.dataset.dtabPanel !== tab.dataset.dtab));
        if (tab.dataset.dtab === 'chunks' && !chunks.loaded) {
          chunks.loaded = true;
          Partials.refreshPiece('chunks_container', {
            query: Object.assign(docQuery(), { page: 1, size: chunks.size })
          });
        }
      });
    });

    bindChunks(document);
    Partials.registerRebinder('chunks_container', bindChunks);

    // 首屏数据：预览与分块按需加载
    Partials.refreshPiece('preview_pane', { query: docQuery() });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
