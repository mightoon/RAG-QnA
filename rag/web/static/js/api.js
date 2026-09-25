/* 统一 API 客户端：Bearer Token 注入、错误规范化、超时控制 */
window.API = (function () {
  'use strict';
  // 各调用点传入的 path 已自带 '/api' 前缀（如 API.post('/api/admin/config')），
  // 此处不能再补一次，否则请求会打到 /api/api/... 而全部 404。
  var BASE = '';
  var TOKEN_KEY = 'iqa_auth_token';

  function getToken() { try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; } }
  function setToken(t) {
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* ignore */ }
    // 同步 Cookie：浏览器页面导航（GET /chat 等）无 Authorization 头，
    // 服务端页面鉴权改读 Cookie rag_token
    try {
      if (t) document.cookie = 'rag_token=' + encodeURIComponent(t) +
        '; path=/; max-age=' + 7 * 24 * 3600 + '; SameSite=Lax';
      else document.cookie = 'rag_token=; path=/; max-age=0; SameSite=Lax';
    } catch (e) { /* ignore */ }
  }

  function ApiError(message, status, network) {
    var e = new Error(message);
    e.name = 'ApiError';
    e.status = status || 0;
    e.network = !!network;
    return e;
  }

  async function request(method, path, opts) {
    opts = opts || {};
    var headers = { 'Accept': 'application/json' };
    var token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    var body = opts.body;
    if (body !== undefined && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    var ctrl = new AbortController();
    var timeout = opts.timeout || 30000;
    var timer = setTimeout(function () { ctrl.abort(); }, timeout);
    var resp;
    try {
      resp = await fetch(BASE + path, {
        method: method, headers: headers,
        body: body, signal: ctrl.signal, credentials: 'same-origin'
      });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') throw ApiError('请求超时（' + (timeout / 1000) + 's）', 0, true);
      throw ApiError('网络连接失败，请检查网络后重试', 0, true);
    }
    clearTimeout(timer);
    if (resp.status === 401 && !opts.noRedirect) {
      setToken(null);
      if (window.Pages && window.Pages.login) window.Pages.login();
      else if (!location.pathname.startsWith('/login')) location.replace('/login');
      throw ApiError('登录已失效，请重新登录', 401);
    }
    if (!resp.ok) {
      var msg = 'HTTP ' + resp.status;
      try {
        var data = await resp.json();
        msg = data.detail || data.error || data.message || msg;
        if (typeof msg !== 'string') msg = JSON.stringify(msg);
      } catch (e) { /* keep status msg */ }
      throw ApiError(msg, resp.status);
    }
    if (opts.raw) return resp;
    var ct = resp.headers.get('content-type') || '';
    if (ct.indexOf('json') >= 0) return resp.json();
    return resp.text();
  }

  return {
    BASE: BASE,
    getToken: getToken,
    setToken: setToken,
    get: function (p, o) { return request('GET', p, o); },
    getData: function (p, o) { return request('GET', p, o); },
    post: function (p, body, o) { return request('POST', p, Object.assign({ body: body }, o)); },
    put: function (p, body, o) { return request('PUT', p, Object.assign({ body: body }, o)); },
    del: function (p, o) { return request('DELETE', p, o); },
    delete: function (p, o) { return request('DELETE', p, o); },
    upload: function (p, formData, o) {
      return request('POST', p, Object.assign({ body: formData, timeout: 120000 }, o));
    },
    /* 下载文件：**必须走带 Token 的 fetch**，不能靠 <a href>。
       历史缺陷：列表里的「下载」用的是 document.createElement('a') + href，
       浏览器导航不携带 Authorization 头 → 后端 401 → "点了没反应/下载失败"；
       而且它指向的是**在线预览**端点（对 PDF 直接 415），本就下不了原文件。
       这里统一用 fetch 取回二进制，再交给浏览器保存，文件名优先取服务端
       Content-Disposition 里的（中文名走 filename*，比前端猜的准）。 */
    download: async function (path, fallbackName) {
      var resp = await request('GET', path, { raw: true, timeout: 300000 });
      var blob = await resp.blob();
      var name = fallbackName || 'download';
      var cd = resp.headers.get('content-disposition') || '';
      var m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
      if (m) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      return name;
    }
  };
})();

/* 基础工具 */
window.utils = {
  escapeHtml: function (s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  },
  formatTime: function (ts) {
    if (!ts) return '';
    var d = new Date(typeof ts === 'number' ? ts : ts);
    if (isNaN(d.getTime())) return String(ts);
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    var hm = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
    if (sameDay) return hm;
    var y = d.getFullYear(), m = ('0' + (d.getMonth() + 1)).slice(-2), dd = ('0' + d.getDate()).slice(-2);
    return (y === now.getFullYear() ? (m + '-' + dd) : (y + '-' + m + '-' + dd)) + ' ' + hm;
  },
  formatSize: function (bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1048576).toFixed(1) + ' MB';
    return (bytes / 1073741824).toFixed(2) + ' GB';
  },
  fileIcon: function (type) {
    var map = { pdf: '📕', docx: '📘', doc: '📘', xlsx: '📗', csv: '📗', pptx: '📙',
      txt: '📄', md: '📄', html: '🌐', png: '🖼️', jpg: '🖼️', jpeg: '🖼️' };
    return map[String(type || '').toLowerCase()] || '📄';
  },
  debounce: function (fn, ms) {
    var t;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms || 300);
    };
  },
  copyText: function (text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy') ? resolve() : reject(new Error('copy failed')); }
      catch (e) { reject(e); }
      document.body.removeChild(ta);
    });
  }
};
