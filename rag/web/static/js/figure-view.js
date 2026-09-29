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

  /* 图区 URL 的版本号：**渲染口径改过就 +1**
   *
   * 为什么需要它：图区端点的响应曾经带 24h 缓存，而渲染**可能**失败并返回一张白图
   * （PDFium 状态异常时不抛异常，见 TS-034）—— 那种白图会被浏览器缓存下来，
   * 于是"服务端修好了，用户还是看到白页"。带上版本号 = 换一批新 URL，旧缓存自然失效。 */
  const FIG_CACHE_V = '2';

  function figUrl(docId, chunkId, dpi) {
    return '/api/documents/' + encodeURIComponent(docId) + '/chunks/'
      + encodeURIComponent(chunkId) + '/figure?dpi=' + (dpi || 170)
      + '&v=' + FIG_CACHE_V;
  }

  function pageUrl(docId, pageNum, chunkId, dpi) {
    return '/api/documents/' + encodeURIComponent(docId) + '/pages/'
      + encodeURIComponent(pageNum) + '/image?dpi=' + (dpi || 90)
      + (chunkId ? '&highlight=' + encodeURIComponent(chunkId) : '')
      + '&v=' + FIG_CACHE_V;
  }

  /* 图加载失败时**去看真实原因**，而不是一律说"没有图区坐标"
   *
   * 为什么必须这样：`<img>` 的 error 事件不带状态码，而失败原因至少有四种，
   * 对应的处置完全不同：
   *   · 401 —— 浏览器发 `<img>` 请求带不了 Authorization 头（只带 Cookie），
   *            服务端要认 Cookie 才行；表现是"所有图都裂"，与数据无关；
   *   · 404「该块没有图区坐标」—— 本次改动前入库的存量块，重解析即可；
   *   · 404「块不存在」—— 文档刚被重解析过，块 id 换了，刷新页面即可；
   *   · 5xx / 网络 —— 服务端或链路问题。
   * 原来一律提示"该块入库时还没有存图区坐标"，用户照着去重解析也修不好（TS-032）。
   */
  async function explainImageFailure(url) {
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (res.ok) return '图片解码失败（接口返回正常，但浏览器读不出这张图）';
      let detail = '';
      try { detail = ((await res.json()) || {}).detail || ''; } catch (e) { /* 非 JSON */ }
      if (res.status === 401 || res.status === 403) {
        return '取图被拒（HTTP ' + res.status + '）：浏览器发图片请求时带不了 Bearer 头，'
          + '服务端需要认 Cookie —— 请升级/重启应用后刷新页面。';
      }
      if (res.status === 404) {
        if (detail.indexOf('图区坐标') >= 0) {
          return '该块没有图区坐标（本次改动前入库的存量块）：重新解析该文档后即可显示。';
        }
        if (detail.indexOf('块不存在') >= 0) {
          return '该分块已不在库里（文档可能刚被重解析过，块 id 变了）：刷新页面后重试。';
        }
        return '取图接口返回 404' + (detail ? '：' + detail : '（接口地址可能不对）');
      }
      if (res.status === 503) {
        // 渲染这一步失败（PDFium 抖动/内存等）：**可重试**，别让用户以为数据有问题
        return '图区渲染失败' + (detail ? '：' + detail : '') + '：稍后重试即可（不影响入库数据）。';
      }
      return '取图失败（HTTP ' + res.status + (detail ? '：' + detail : '') + '）';
    } catch (e) {
      return '取图失败（网络错误）：' + (e && e.message ? e.message : e);
    }
  }

  function replaceWithNote(img, msg) {
    const box = document.createElement('div');
    box.className = 'figure-unavailable';
    box.textContent = msg;
    if (img && img.parentNode) img.parentNode.replaceChild(box, img);
    return box;
  }

  /* 给所有"图区"图片挂一次失败诊断（裂图 → 一句能照着处置的说明） */
  function attach(root) {
    const scope = root || document;
    const imgs = scope.querySelectorAll
      ? scope.querySelectorAll('img[data-figure-src], img.chunk-figure-thumb,'
                               + ' img.source-item-figure img, img.figure-view-img')
      : [];
    Array.prototype.forEach.call(imgs, img => {
      if (img.dataset.figureDiag === '1') return;
      img.dataset.figureDiag = '1';
      img.addEventListener('error', async () => {
        const url = img.dataset.figureSrc || img.getAttribute('src') || '';
        if (!url) return;
        img.dataset.figureDiag = '2';                    // 防止重复触发
        replaceWithNote(img, await explainImageFailure(url));
      });
    });
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
    // 图取不到时**去看真实原因**（401/404 各有一套处置），把原因写在图原来的位置上，
    // 而不是留一个破图图标 + 一句笼统的"没有图区坐标"（用户实测照着修不好，见 TS-032）
    attach(overlay);
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => attach(document));
  } else {
    attach(document);
  }

  window.FigureView = { open, close, attach, figUrl, pageUrl };
})();
