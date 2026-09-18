/* 来源抽屉：消息引用列表 → 右侧滑出面板 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);

  function esc(s) { return window.utils.escapeHtml(s); }

  function sourceItemHtml(src, i) {
    const title = src.filename || src.doc_name || src.doc_id || ('来源 ' + (i + 1));
    const loc = [];
    if (src.page != null) loc.push('P' + src.page);
    if (src.section_path) loc.push(String(src.section_path).split('/').pop());
    if (src.collection) loc.push(src.collection);
    const score = (src.score != null) ? ('相关度 ' + Number(src.score).toFixed(2)) : '';
    return `<div class="source-item" data-idx="${i}">
      <div class="source-item-head">
        <span class="badge badge-info">[${i + 1}]</span>
        <span class="source-item-title">${esc(title)}</span>
      </div>
      <div class="source-item-loc">${esc(loc.join(' · '))}${loc.length && score ? ' · ' : ''}${esc(score)}</div>
      ${src.text ? `<div class="source-item-loc" style="margin-top:6px">${esc(String(src.text).slice(0, 240))}${String(src.text).length > 240 ? '…' : ''}</div>` : ''}
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
