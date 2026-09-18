/* 聊天 UI 渲染：消息气泡 / Markdown / 反馈 / 打字指示 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const showToast = (msg, type) => {
    if (window.Partials && window.Partials.showToast) window.Partials.showToast(msg, type);
    else console.log('[toast]', type, msg);
  };

  function renderMarkdown(text) {
    let html;
    try { html = window.marked ? marked.parse(text || '') : null; }
    catch (e) { html = null; }
    if (html == null) html = window.utils.escapeHtml(text || '').replace(/\n/g, '<br>');
    if (window.DOMPurify) html = DOMPurify.sanitize(html);
    return html;
  }

  function scrollToBottom() {
    const sc = qs('#chat-scroll');
    if (sc) sc.scrollTop = sc.scrollHeight;
  }

  function hideEmpty() {
    const e = qs('#chat-empty');
    if (e) e.remove();
  }

  function addUserMessage(text) {
    hideEmpty();
    const wrap = qs('#messages-container');
    const div = document.createElement('div');
    div.className = 'msg-user';
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    bubble.textContent = text;
    div.appendChild(bubble);
    wrap.appendChild(div);
    scrollToBottom();
    return div;
  }

  function addAssistantShell() {
    hideEmpty();
    const wrap = qs('#messages-container');
    const div = document.createElement('div');
    div.className = 'msg-assistant';
    div.innerHTML = `
      <div class="assistant-avatar">AI</div>
      <div class="msg-content-col">
        <div class="msg-block prose streaming"></div>
        <div class="msg-footer"></div>
      </div>`;
    wrap.appendChild(div);
    scrollToBottom();
    return {
      root: div,
      block: div.querySelector('.msg-block'),
      footer: div.querySelector('.msg-footer'),
      text: '',
      sources: [],
      appendToken(t) {
        this.text += t;
        this.block.innerHTML = renderMarkdown(this.text);
        scrollToBottom();
      },
      finalize(messageId) {
        this.block.classList.remove('streaming');
        this.block.innerHTML = renderMarkdown(this.text);
        if (messageId) this.root.dataset.msgId = messageId;
        renderFeedback(this.footer, messageId);
        renderSourcesStrip(this.footer, this.sources);
        scrollToBottom();
      },
      fail(msg) {
        this.block.classList.remove('streaming');
        this.block.innerHTML = '<span class="text-error"></span>';
        this.block.querySelector('.text-error').textContent = '生成失败：' + msg;
        scrollToBottom();
      }
    };
  }

  function renderSourcesStrip(footer, sources) {
    if (!sources || !sources.length) return;
    const strip = document.createElement('div');
    strip.className = 'sources-strip';
    strip.dataset.sources = JSON.stringify(sources);
    strip.textContent = `📚 ${sources.length} 个来源`;
    footer.appendChild(strip);
  }

  function renderFeedback(footer, messageId) {
    if (!messageId) return;
    const bar = document.createElement('div');
    bar.className = 'feedback-bar';
    bar.innerHTML = `
      <button class="btn btn-ghost btn-xs feedback-btn" data-fb="up" title="有帮助">👍</button>
      <button class="btn btn-ghost btn-xs feedback-btn" data-fb="down" title="无帮助">👎</button>
      <button class="btn btn-ghost btn-xs" data-act="copy">复制</button>`;
    footer.appendChild(bar);
    bar.addEventListener('click', async e => {
      const fbBtn = e.target.closest('[data-fb]');
      if (fbBtn) {
        bar.querySelectorAll('.feedback-btn').forEach(b => b.classList.remove('active'));
        fbBtn.classList.add('active');
        try {
          await API.post('/api/feedback', {
            session_id: window.ChatPage && window.ChatPage.sessionId,
            message_id: messageId, feedback: fbBtn.dataset.fb
          });
        } catch (err) { showToast('反馈提交失败', 'warning'); }
        return;
      }
      if (e.target.closest('[data-act="copy"]')) {
        const block = footer.parentElement.querySelector('.msg-block');
        window.utils.copyText(block ? block.innerText : '')
          .then(() => showToast('已复制', 'success'))
          .catch(() => showToast('复制失败', 'warning'));
      }
    });
  }

  /* SSR 消息：把 data-md 渲染为 Markdown，data-sources 已委托给 chat-sources */
  function hydrateServerMessages(root) {
    (root || document).querySelectorAll('.msg-block[data-md]').forEach(el => {
      const md = el.dataset.md || '';
      el.removeAttribute('data-md');
      el.innerHTML = renderMarkdown(md);
    });
  }

  function showTyping() {
    const wrap = qs('#messages-container');
    const div = document.createElement('div');
    div.className = 'msg-assistant';
    div.id = 'typing-row';
    div.innerHTML = '<div class="assistant-avatar">AI</div>' +
      '<div class="msg-content-col"><div class="msg-block"><div class="typing-indicator"><span></span><span></span><span></span></div></div></div>';
    wrap.appendChild(div);
    scrollToBottom();
  }
  function hideTyping() {
    const t = qs('#typing-row');
    if (t) t.remove();
  }

  function showError(msg) {
    const strip = qs('#chat-error');
    if (!strip) { showToast(msg, 'error'); return; }
    qs('#chat-error-text').textContent = msg;
    strip.classList.remove('hidden');
  }

  window.ChatUI = {
    renderMarkdown, scrollToBottom, addUserMessage, addAssistantShell,
    hydrateServerMessages, showTyping, hideTyping, showError
  };
})();
