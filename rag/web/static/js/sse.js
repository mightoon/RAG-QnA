/* SSE 客户端：EventSource（GET）+ fetch ReadableStream（POST 流式）双通道 */
window.SSEClient = (function () {
  'use strict';

  /* POST 流式（chat query）：解析 SSE 帧 → onEvent(type, data) */
  async function stream(url, payload, handlers) {
    handlers = handlers || {};
    var headers = { 'Accept': 'text/event-stream', 'Content-Type': 'application/json' };
    var token = window.API.getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;
    var resp;
    try {
      resp = await fetch(url, {
        method: 'POST', headers: headers,
        body: JSON.stringify(payload), credentials: 'same-origin'
      });
    } catch (e) {
      handlers.onError && handlers.onError({ type: 'network', message: '网络连接失败，请检查网络' });
      return;
    }
    if (!resp.ok || !resp.body) {
      var msg = 'HTTP ' + resp.status;
      try {
        var data = await resp.json();
        msg = data.detail || msg;
      } catch (e) { /* ignore */ }
      handlers.onError && handlers.onError({ type: 'http', status: resp.status, message: msg });
      return;
    }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buf = '';

    function dispatch(frame) {
      var type = 'message', dataLines = [];
      frame.split('\n').forEach(function (line) {
        if (line.indexOf('event:') === 0) type = line.slice(6).trim();
        else if (line.indexOf('data:') === 0) dataLines.push(line.slice(5));
      });
      if (!dataLines.length) return;
      var raw = dataLines.join('\n').trim();
      var data;
      try { data = JSON.parse(raw); } catch (e) { data = { text: raw }; }
      if (data && data.type && !data._t) data._t = type;
      handlers.onEvent && handlers.onEvent(data.type || type, data);
    }

    while (true) {
      var chunk;
      try { chunk = await reader.read(); }
      catch (e) { handlers.onError && handlers.onError({ type: 'network', message: '连接中断' }); return; }
      if (chunk.done) break;
      handlers.onProgress && handlers.onProgress();
      buf += decoder.decode(chunk.value, { stream: true });
      var idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        var frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (frame.trim()) dispatch(frame);
      }
    }
    if (buf.trim()) dispatch(buf);
    handlers.onEnd && handlers.onEnd();
  }

  /* GET EventSource（进度推送）：自动重连由浏览器负责；
     maxGapMs 内无事件触发 onStall 供调用方决定降级 */
  function listen(url, handlers, maxGapMs) {
    handlers = handlers || {};
    var es = null, closed = false, stallTimer = null;
    try { es = new EventSource(url); }
    catch (e) { handlers.onError && handlers.onError(e); return { close: function () {} }; }

    function armStall() {
      clearTimeout(stallTimer);
      if (maxGapMs) {
        stallTimer = setTimeout(function () {
          handlers.onStall && handlers.onStall();
        }, maxGapMs);
      }
    }
    es.onmessage = function (ev) {
      armStall();
      try { handlers.onMessage && handlers.onMessage(JSON.parse(ev.data)); }
      catch (e) { /* 非 JSON 帧忽略 */ }
    };
    es.onerror = function (ev) { handlers.onError && handlers.onError(ev); };
    armStall();
    return {
      close: function () {
        if (closed) return;
        closed = true;
        clearTimeout(stallTimer);
        try { es && es.close(); } catch (e) { /* ignore */ }
      }
    };
  }

  return { stream: stream, listen: listen };
})();
