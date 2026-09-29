/* 来源抽屉：消息引用列表 → 右侧滑出面板 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);

  function esc(s) { return window.utils.escapeHtml(s); }

  /* 来源条目的字段必须与后端 SourceReference 的**实际键名**对齐
     （`s.model_dump()` 出来的键：ref_id/chunk_id/doc_id/title/section/section_path/
      page_num/page_end/figure_label/figure_caption/storage_url/preview_url/chunk_type）。
     历史缺陷：这里读的是 `page`/`filename`/`text`/`score` —— 后端一个都没有，
     于是来源抽屉里**从不显示页码**、标题退化成内部 doc_id（`doc_xxxx`），
     摘录与相关度永远是空。 */
  function sourceItemHtml(src, i) {
    const title = src.title || src.filename || src.doc_name || src.doc_id
      || ('来源 ' + (i + 1));
    const loc = [];
    // 页码：跨页的块显示 P3–4（page_end 由检索步骤从 MySQL 补上）
    if (src.page_num != null) {
      loc.push(src.page_end && src.page_end > src.page_num
        ? `P${src.page_num}–${src.page_end}`
        : `P${src.page_num}`);
    }
    if (src.figure_label) loc.push(src.figure_label);
    if (src.section_path) loc.push(String(src.section_path).split('/').pop());
    const excerpt = src.text || src.snippet
      ? String(src.text || src.snippet).slice(0, 240) : '';
    /* 图块（image_caption）额外显示"这张图"本身：库里存的是图区坐标（PDF point），
       图片按需从原始 PDF 现裁（见 figure-view.js）。点一下能看它在页面上的位置。
       老块没有 figure_bbox（本次改动前入库的）→ 不显示图片，只留文字摘录。 */
    const isFigure = src.chunk_type === 'image_caption' || !!src.figure_bbox;
    const fig = isFigure && src.doc_id && src.chunk_id
      ? `<div class="source-item-figure">
           <img loading="lazy" alt="图区" style="max-width:100%;cursor:zoom-in"
                data-figure-src="/api/documents/${encodeURIComponent(src.doc_id)}/chunks/${encodeURIComponent(src.chunk_id)}/figure?dpi=140"
                src="/api/documents/${encodeURIComponent(src.doc_id)}/chunks/${encodeURIComponent(src.chunk_id)}/figure?dpi=140"
                data-figure-doc="${esc(src.doc_id)}" data-figure-chunk="${esc(src.chunk_id)}"
                data-figure-page="${src.page_num || ''}"
                data-figure-label="${esc(src.figure_label || '')}"
                data-figure-caption="${esc(src.figure_caption || '')}">
           <div>点图看它在页面上的位置（红框）</div>
         </div>` : '';
    return `<div class="source-item" data-idx="${i}">
      <div class="source-item-head">
        <span class="badge badge-info">[${i + 1}]</span>
        <span class="source-item-title">${esc(title)}</span>
      </div>
      <div class="source-item-loc">${esc(loc.join(' · '))}</div>
      ${excerpt ? `<div class="source-item-loc" style="margin-top:6px">${esc(excerpt)}${String(src.text || src.snippet).length > 240 ? '…' : ''}</div>` : ''}
      ${fig}
    </div>`;
  }

  function open(sources) {
    close();
    const overlay = document.createElement('div');
    overlay.className = 'sources-drawer-overlay';
    const drawer = document.createElement('div');
    drawer.className = 'sources-drawer';
    drawer.innerHTML = `
      <div class="sources-drawer-header">
        <span>引用来源（${sources.length}）</span>
        <button class="icon-btn" aria-label="关闭">✕</button>
      </div>
      <div class="sources-drawer-body">
        ${sources.map(sourceItemHtml).join('') || '<div class="table-empty">无来源</div>'}
      </div>`;
    overlay.appendChild(drawer);
    document.body.appendChild(overlay);
    // 抽屉里的图区图也走同一套失败诊断（401/404 各有一套处置，见 figure-view.js）
    if (window.FigureView && window.FigureView.attach) window.FigureView.attach(drawer);
    const doClose = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) doClose(); });
    qs('.icon-btn', drawer).addEventListener('click', doClose);
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') { doClose(); document.removeEventListener('keydown', onKey); }
    });
  }

  function close() {
    const old = qs('.sources-drawer-overlay');
    if (old) old.remove();
  }

  /* 事件委托：点击 sources-strip 打开抽屉 */
  document.addEventListener('click', e => {
    const strip = e.target.closest && e.target.closest('.sources-strip');
    if (!strip) return;
    try {
      const sources = JSON.parse(strip.dataset.sources || '[]');
      if (sources.length) open(sources);
    } catch (err) { /* ignore */ }
  });

  window.ChatSources = { open, close };
})();
