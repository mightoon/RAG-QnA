/* 图区查看器：把"检索命中的那张图"显示出来，并在整页图上用红框标出它的位置
 *
 * 为什么需要它：图片字节**不落库**（MinIO 里只有原始 PDF），库里存的是图区的
 * PDF point 框。所以"看图"= 拿框去后端现裁（/chunks/{id}/figure），"看位置"=
 * 后端整页渲染时顺手把框画上（/pages/{n}/image?highlight={id}）。
 * 两个接口都在服务端用同一份坐标，前端不做任何几何计算 —— 缩放比例算错会让框
 * 偏移，而偏移在界面看起来"像是对的"，属于最难发现的一类错误。
 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);

  function esc(s) { return window.utils ? window.utils.escapeHtml(s) : String(s || ''); }

  function figUrl(docId, chunkId, dpi) {
    return '/api/documents/' + encodeURIComponent(docId) + '/chunks/'
      + encodeURIComponent(chunkId) + '/figure?dpi=' + (dpi || 170);
  }

  function pageUrl(docId, pageNum, chunkId, dpi) {
    return '/api/documents/' + encodeURIComponent(docId) + '/pages/'
      + encodeURIComponent(pageNum) + '/image?dpi=' + (dpi || 90)
      + (chunkId ? '&highlight=' + encodeURIComponent(chunkId) : '');
  }

  /* 打开查看器：区域图 + 整页（带红框）。
     opts = {docId, chunkId, page, caption, label} */
  function open(opts) {
    close();
    const { docId, chunkId, page } = opts || {};
    if (!docId || !chunkId) return;
    const overlay = document.createElement('div');
    overlay.className = 'figure-view-overlay';
    const title = [opts.label, opts.caption].filter(Boolean).join(' · ');
    overlay.innerHTML = `
      <div class="figure-view">
        <div class="figure-view-head">
          <span class="figure-view-title">${esc(title || '图区')}</span>
          <span class="figure-view-loc">${page ? '第 ' + page + ' 页' : ''}</span>
          <button class="icon-btn" type="button" aria-label="关闭">✕</button>
        </div>
        <div class="figure-view-body">
          <div class="figure-view-col">
            <div class="figure-view-cap">图区（按库里存的坐标从原始 PDF 现裁）</div>
            <img class="figure-view-img" alt="图区"
                 src="${figUrl(docId, chunkId)}">
            <div class="figure-view-err hidden"></div>
          </div>
          ${page ? `<div class="figure-view-col">
            <div class="figure-view-cap">它在页面上的位置（红框）</div>
            <img class="figure-view-img" alt="页面" loading="lazy"
                 src="${pageUrl(docId, page, chunkId)}">
          </div>` : ''}
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const doClose = () => overlay.remove();
    overlay.addEventListener('click', e => { if (e.target === overlay) doClose(); });
    qs('.icon-btn', overlay).addEventListener('click', doClose);
    document.addEventListener('keydown', function onKey(e) {
      if (e.key === 'Escape') { doClose(); document.removeEventListener('keydown', onKey); }
    });
    // 存量块没有图区坐标（本次改动前入库的）→ 后端 404，这里如实说清楚怎么办，
    // 而不是留一个破图图标让用户猜
    const img = qs('.figure-view-img', overlay);
    img.addEventListener('error', () => {
      img.classList.add('hidden');
      const err = qs('.figure-view-err', overlay);
      err.classList.remove('hidden');
      err.textContent = '取不到图区：该块入库时还没有存图区坐标（重新解析该文档后即可显示）。';
    });
  }

  function close() {
    const old = qs('.figure-view-overlay');
    if (old) old.remove();
  }

  // 事件委托：任何带 data-figure-* 的元素点了就开
  document.addEventListener('click', e => {
    const el = e.target.closest && e.target.closest('[data-figure-doc]');
    if (!el) return;
    e.preventDefault();
    open({
      docId: el.dataset.figureDoc,
      chunkId: el.dataset.figureChunk,
      page: el.dataset.figurePage ? parseInt(el.dataset.figurePage, 10) : null,
      label: el.dataset.figureLabel || '',
      caption: el.dataset.figureCaption || '',
    });
  });

  window.FigureView = { open, close, figUrl, pageUrl };
})();
