/* 统一局部刷新系统
   - <template id="..."> 声明 + 命名空间注册（window.Partials.use(ns)）
   - 标准生命周期：prepare/render/hydrate/onError
   - Fragment 挂载、两阶段错误隔离、弱网降级
*/
window.Partials = (function () {
  'use strict';
  var namespaces = {};

  function ensureToastContainer() {
    var c = document.querySelector('.toast-container');
    if (!c) {
      c = document.createElement('div');
      c.className = 'toast-container';
      document.body.appendChild(c);
    }
    return c;
  }

  function showToast(message, type, duration) {
    type = type || 'info';
    var c = ensureToastContainer();
    var t = document.createElement('div');
    t.className = 'toast ' + type;
    var msgSpan = document.createElement('span');
    msgSpan.className = 'toast-message';
    msgSpan.textContent = message;
    t.appendChild(msgSpan);
    /* 常驻 = 一定要用户点了才走，只有它才给 ✕。
       自动消失的提示不给 ✕（成功的提示自己会走，别让用户多操心一个按钮）。 */
    var persistent = (typeof duration === 'number')
      ? duration === 0
      : (type === 'error' || type === 'danger');
    var ttl = (typeof duration === 'number') ? duration
            : (persistent ? 0 : (type === 'success' ? 2000 : 3000));
    if (persistent) {
      var close = document.createElement('button');
      close.className = 'toast-close';
      close.textContent = '✕';
      close.title = '关闭';
      close.setAttribute('aria-label', '关闭');
      close.addEventListener('click', function () { t.remove(); });
      t.appendChild(close);
    }
    c.appendChild(t);
    if (ttl > 0) setTimeout(function () { if (t.parentNode) t.remove(); }, ttl);
  }

  window.showToast = showToast;

  /* ── 模态框构建 ── */
  function buildModal(opts) {
    opts = opts || {};
    var overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    var content = document.createElement('div');
    content.className = 'modal-content' + (opts.type ? ' modal-' + opts.type : '');
    var header = document.createElement('div');
    header.className = 'modal-header';
    var titleWrap = document.createElement('div');
    titleWrap.className = 'modal-title';
    var h3 = document.createElement('h3');
    h3.textContent = opts.title || '提示';
    titleWrap.appendChild(h3);
    if (opts.subtitle) {
      var sub = document.createElement('div');
      sub.className = 'modal-sub';
      sub.textContent = opts.subtitle;
      titleWrap.appendChild(sub);
    }
    header.appendChild(titleWrap);
    /* headerActions：调用方要塞进标题这一行的操作（一个节点或一组），摆到 ✕
       左边 —— 与关闭键绑成同一组、"同一水平线"，标题再长也挤不动它。
       模型编辑弹窗用它把「测试模型 / 更新模型库」从表单里提上来（那边与标题重复） */
    var headRight = document.createElement('div');
    headRight.className = 'modal-header-right';
    if (opts.headerActions) {
      var acts = Array.isArray(opts.headerActions) ? opts.headerActions
                                                   : [opts.headerActions];
      acts.forEach(function (n) { if (n) headRight.appendChild(n); });
    }
    var closeBtn = document.createElement('button');
    closeBtn.className = 'icon-btn';
    closeBtn.textContent = '✕';
    closeBtn.setAttribute('aria-label', '关闭');
    headRight.appendChild(closeBtn);
    header.appendChild(headRight);
    content.appendChild(header);
    var body = document.createElement('div');
    body.className = 'modal-body';
    if (opts.body instanceof Node) body.appendChild(opts.body);
    else if (opts.bodyHtml) body.innerHTML = opts.bodyHtml;
    else {
      var p = document.createElement('p');
      p.className = 'modal-text' + (opts.preserveWhitespace ? ' preserve' : '');
      if (opts.allowHtml) p.innerHTML = opts.message || '';
      else p.textContent = opts.message || '';
      body.appendChild(p);
    }
    content.appendChild(body);
    if (opts.footer) content.appendChild(opts.footer);
    overlay.appendChild(content);

    function close(remember) {
      if (overlay.parentNode) overlay.remove();
      document.removeEventListener('keydown', onKey);
      if (opts.storageKey && remember === true) {
        try { localStorage.setItem(opts.storageKey, '1'); } catch (e) { /* ignore */ }
      }
      opts.onClose && opts.onClose();
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    closeBtn.addEventListener('click', function () { close(); });
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
    document.addEventListener('keydown', onKey);
    if (opts.storageKey) {
      try {
        if (localStorage.getItem(opts.storageKey)) return null;
      } catch (e) { /* ignore */ }
    }
    document.body.appendChild(overlay);
    return { overlay: overlay, body: body, close: close };
  }

  /* 确认框：warning / danger 双模式；danger 需键入确认词 */
  function confirm(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var body = document.createElement('div');
      if (opts.detail) {
        var warn = document.createElement('div');
        warn.className = 'modal-warning-box' + (opts.type === 'danger' ? ' danger' : '');
        warn.textContent = opts.detail;
        body.appendChild(warn);
      }
      var p = document.createElement('p');
      p.className = 'modal-text' + (opts.type === 'danger' ? ' danger-text' : ' preserve');
      p.textContent = opts.message || '确认执行该操作？';
      body.appendChild(p);
      var input = null;
      if (opts.type === 'danger' && opts.confirmWord) {
        var g = document.createElement('div');
        g.className = 'form-group';
        g.style.marginTop = '14px';
        var lab = document.createElement('label');
        lab.className = 'form-label';
        lab.innerHTML = '请输入 <b>' + window.utils.escapeHtml(opts.confirmWord) + '</b> 以确认';
        g.appendChild(lab);
        input = document.createElement('input');
        input.className = 'form-input';
        input.placeholder = opts.confirmWord;
        g.appendChild(input);
        body.appendChild(g);
      }
      var footer = document.createElement('div');
      footer.className = 'modal-footer';
      var cancel = document.createElement('button');
      cancel.className = 'btn btn-secondary';
      cancel.textContent = opts.cancelText || '取消';
      var ok = document.createElement('button');
      ok.className = 'btn ' + (opts.type === 'danger' ? 'btn-danger' : 'btn-primary');
      ok.textContent = opts.confirmText || (opts.type === 'danger' ? '永久删除' : '确认');
      if (input) ok.disabled = true;
      footer.appendChild(cancel);
      footer.appendChild(ok);
      var m = buildModal({
        title: opts.title || '确认', body: body, footer: footer,
        type: opts.type === 'danger' ? 'danger' : 'warning',
        onClose: function () { resolve(false); }
      });
      if (!m) { resolve(false); return; }
      cancel.addEventListener('click', function () { m.close(); resolve(false); });
      if (input) {
        input.addEventListener('input', function () {
          ok.disabled = input.value.trim() !== opts.confirmWord;
        });
        setTimeout(function () { input.focus(); }, 50);
      }
      ok.addEventListener('click', function () { m.close(); resolve(true); });
    });
  }

  window.confirmDialog = confirm;

  /* ── 命名空间注册 ── */
  function use(ns) {
    if (!namespaces[ns]) namespaces[ns] = { templates: {}, renderers: {}, hydraters: {} };
    return namespaces[ns];
  }

  function registerTemplates(ns, templates) {
    var space = use(ns);
    Object.keys(templates).forEach(function (key) {
      space.templates[key] = templates[key];
    });
  }

  function registerRenderer(ns, key, fn) { use(ns).renderers[key] = fn; }
  function registerHydrater(ns, key, fn) { use(ns).hydraters[key] = fn; }

  /* 从模板克隆渲染：template HTML → 元素；renderer 填充数据 */
  function renderTemplate(ns, key, data) {
    var space = use(ns);
    var html = space.templates[key];
    if (!html) { console.error('[Partials] 未注册模板: ' + ns + '.' + key); return null; }
    var tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    var el = tpl.content.firstElementChild;
    if (space.renderers[key]) {
      try { space.renderers[key](el, data); }
      catch (e) { console.error('[Partials] 渲染失败 ' + ns + '.' + key, e); }
    }
    return el;
  }

  function hydrate(ns, key, el, data) {
    var space = use(ns);
    if (space.hydraters[key]) {
      try { space.hydraters[key](el, data); }
      catch (e) { console.error('[Partials] 水合失败 ' + ns + '.' + key, e); }
    }
  }

  /* 通用列表刷新：容器 + 条目数组 + 生命周期 */
  function refreshList(container, items, opts) {
    opts = opts || {};
    container.innerHTML = '';
    if (!items || !items.length) {
      if (opts.emptyHtml) container.innerHTML = opts.emptyHtml;
      else if (opts.emptyText) {
        var e = document.createElement('div');
        e.className = 'table-empty';
        e.textContent = opts.emptyText;
        container.appendChild(e);
      }
      return 0;
    }
    var frag = document.createDocumentFragment();
    var prepared = items;
    if (opts.prepare) prepared = items.map(opts.prepare).filter(Boolean);
    var count = 0;
    prepared.forEach(function (item, i) {
      var node;
      try { node = opts.render(item, i); }
      catch (e) {
        console.error('[Partials] 条目渲染失败', e);
        if (opts.onError) opts.onError(e, item);
        return;
      }
      if (!node) return;
      frag.appendChild(node);
      count++;
    });
    container.appendChild(frag);
    if (opts.hydrate) {
      Array.prototype.forEach.call(container.children, function (el, i) {
        try { opts.hydrate(el, prepared[i], i); } catch (e) { console.error(e); }
      });
    }
    return count;
  }

  /* ── Piece 局部刷新系统 ──
     数据源优先级：opts.data > 注册的 dataLoader > 服务端 /api/ui/piece/{page}/{piece}（返回 {html}）
     渲染优先级：注册的 renderer > 客户端模板 partials::{page}::{piece} > data.__html > data.html
  */
  var dataLoaders = {};
  var rebinders = {};
  var pieceRenderers = {};

  function registerDataLoader(piece, fn) { dataLoaders[piece] = fn; }
  function registerRebinder(piece, fn) {
    if (!rebinders[piece]) rebinders[piece] = [];
    rebinders[piece].push(fn);
  }
  function registerPieceRenderer(piece, fn) { pieceRenderers[piece] = fn; }

  function runRebinders(piece, root) {
    (rebinders[piece] || []).forEach(function (fn) {
      try { fn(root); } catch (e) { console.error('[Partials] 重绑定失败 ' + piece, e); }
    });
    // 嵌套 piece 的重绑定
    Array.prototype.forEach.call(root.querySelectorAll('[data-piece]'), function (sub) {
      runRebinders(sub.dataset.piece, sub);
    });
  }

  async function loadPieceData(piece, opts) {
    if (dataLoaders[piece]) return (await dataLoaders[piece](opts)) || {};
    var page = document.body.dataset.page;
    var qs = '';
    if (opts.query) {
      var params = new URLSearchParams();
      Object.keys(opts.query).forEach(function (k) {
        if (opts.query[k] !== undefined && opts.query[k] !== null && opts.query[k] !== '') {
          params.set(k, opts.query[k]);
        }
      });
      var s = params.toString();
      if (s) qs = '?' + s;
    }
    return window.API.getData('/api/ui/piece/' + page + '/' + piece + qs);
  }

  /* 把渲染结果写入 piece 容器。
     渲染器/模板返回的是「已绑定事件」的 DOM 节点，必须直接挂载；
     早期实现把它序列化成 HTML 字符串再经 innerHTML 解析，
     会重建出一批没有任何监听器的新节点，导致渲染器内部的
     addEventListener 全部失效（配置页「测试连接」点击无反应即由此而来）。
     只有服务端下发的 HTML（__html/html）才走 innerHTML 解析。 */
  function renderPieceInto(root, piece, data) {
    if (pieceRenderers[piece]) {
      var node = pieceRenderers[piece](data);
      if (node instanceof Node) {
        while (root.firstChild) root.removeChild(root.firstChild);
        root.appendChild(node);
        return;
      }
      root.innerHTML = String(node == null ? '' : node);
      return;
    }
    var page = document.body.dataset.page;
    var space = namespaces[page];
    if (space && space.templates[piece]) {
      var el = renderTemplate(page, piece, data);
      if (el) {
        while (root.firstChild) root.removeChild(root.firstChild);
        root.appendChild(el);
        return;
      }
    }
    if (data && data.__html != null) root.innerHTML = data.__html;
    else if (data && data.html != null) root.innerHTML = data.html;
  }

  /* 渲染（数据已知）：不请求数据，直接渲染到 piece 容器 */
  function renderPiece(name, data, opts) {
    opts = opts || {};
    var root = opts.root || document.querySelector('[data-piece="' + name + '"]');
    if (!root) return null;
    renderPieceInto(root, name, data || {});
    runRebinders(name, root);
    return root;
  }

  /* 刷新（数据未知）：加载数据 → 渲染 → 重绑定；失败回退 */
  async function refreshPiece(name, opts) {
    opts = opts || {};
    var root = opts.root || document.querySelector('[data-piece="' + name + '"]');
    if (!root) return null;
    var data = opts.data;
    if (data === undefined) {
      root.classList.add('piece-loading');
      try {
        data = await loadPieceData(name, opts);
      } catch (e) {
        root.classList.remove('piece-loading');
        console.error('[Partials] piece 加载失败 ' + name, e);
        if (opts.onError) { opts.onError(e, root); return null; }
        root.innerHTML = '<div class="piece-error-bar"><span>加载失败：' +
          window.utils.escapeHtml(e.message || String(e)) + '</span>' +
          '<button class="btn btn-ghost btn-xs" type="button">重试</button></div>';
        var btn = root.querySelector('button');
        if (btn) btn.addEventListener('click', function () { refreshPiece(name, opts); });
        return null;
      }
      root.classList.remove('piece-loading');
    }
    renderPieceInto(root, name, data || {});
    runRebinders(name, root);
    if (opts.onDone) opts.onDone(root, data);
    return root;
  }

  function invalidateAndRefresh(names, opts) {
    return Promise.all((names || []).map(function (n) {
      return refreshPiece(n, opts);
    }));
  }

  return {
    use: use,
    registerTemplates: registerTemplates,
    registerRenderer: registerRenderer,
    registerHydrater: registerHydrater,
    renderTemplate: renderTemplate,
    hydrate: hydrate,
    refreshList: refreshList,
    registerDataLoader: registerDataLoader,
    registerRebinder: registerRebinder,
    registerPieceRenderer: registerPieceRenderer,
    renderPiece: renderPiece,
    refreshPiece: refreshPiece,
    invalidateAndRefresh: invalidateAndRefresh,
    showToast: showToast,
    modal: buildModal,
    confirm: confirm
  };
})();
