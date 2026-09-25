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

  /* 处理中实时刷新状态：文档行只在"提交"与"收尾"两次落库，中间阶段由任务行提供
     （后端 `_active_status_by_doc` 已把它盖到 doc.status 上）。这里在**活动态**期间
     每 5 秒刷一次头部，终止态自动停 —— 否则详情页会一直停在"排队中"，
     直到用户手动刷新才跳到"已完成"。 */
  const ACTIVE = ['pending', 'parsing', 'chunking', 'embedding', 'writing', 'retrying'];
  let pollTimer = null;
  function ensurePoll() {
    const row = document.querySelector('.doc-title-row');
    const st = row ? (row.dataset.docStatus || '') : '';
    const active = ACTIVE.indexOf(st) >= 0;
    if (active && !pollTimer) {
      pollTimer = setInterval(refreshHeaderAttrs, 5000);
    } else if (!active && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    return active;
  }

  /* ── 操作 ──
     ⚠ 详情页的动作必须与知识库列表保持同一套实现：
     「下载」曾用 <a href="/content"> —— 浏览器导航不带 Authorization（401
     "需要获得授权"），而且 /content 是**预览**端点、PDF 直接 415；
     「删除」这里原来直接调 `/permanent`（彻底删除），而列表里同一个词是"移入
     回收站" —— 同一个动作名在两个页面干两件不可互换的事，是本次修掉的第二个
     不一致（第一个是下载）。现在两处都是：删除 = 移入回收站。 */
  async function doOp(op, filename) {
    const id = encodeURIComponent(docId);
    try {
      if (op === 'reingest') {
        await API.post('/api/ingest/documents/' + id + '/reingest', {});
        showToast('已开始重解析（沿用原文档，稍后刷新看进度）', 'success');
        refreshHeaderAttrs();
        Partials.refreshPiece('preview_pane', { query: docQuery() });
      } else if (op === 'download') {
        // 带 Token 的 fetch 取二进制再保存（见 api.js 的 API.download）
        await API.download('/api/documents/' + id + '/download', filename);
        showToast('已开始下载：' + (filename || ''), 'success');
      } else if (op === 'delete') {
        /* 删除 = 移入回收站（与知识库列表同一个语义、同一个接口）。
           历史缺陷：这里原来直接走 `/permanent`（彻底删除），而列表里是软删除，
           同一个词在两个页面干的是两件事 —— 在详情页点「删除」是不可恢复的。 */
        const ok = await confirmDialog({
          title: '移入回收站',
          message: `将「${filename}」移入回收站，文档列表里不再显示。`,
          detail: '回收站中的文档不参与检索，内容仍在各数据库中保存；'
                + '可在回收站恢复，或勾选后彻底删除。',
          confirmText: '移入回收站',
        });
        if (!ok) return;
        await API.delete('/api/documents/' + id);
        showToast('已移入回收站', 'success');
        // 重新加载本页：头部徽标与操作按钮由服务端按状态渲染（回收站中 → 恢复/彻底删除）
        location.reload();
      } else if (op === 'restore') {
        const r = await API.post('/api/documents/' + id + '/restore', {});
        showToast('已恢复到列表（状态：' + ((r && r.restoredTo) || 'done') + '）', 'success');
        location.reload();
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
      btn.addEventListener('click', () => doOp(btn.dataset.docOp, filename));    });

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
    // 处理中（重解析/入库进行中）时每 5 秒刷新状态，终止后自动停
    ensurePoll();
    setInterval(ensurePoll, 5000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
