/* 聊天主控制器：会话管理 / 检索路选择 / SSE 流式问答 / 临时文档 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const qsa = (s, r) => Array.from((r || document).querySelectorAll(s));
  const showToast = (msg, type) => {
    if (window.Partials && window.Partials.showToast) window.Partials.showToast(msg, type);
    else console.log('[toast]', type, msg);
  };

  const state = {
    sessionId: '',
    sending: false,
    paths: [],
  };
  window.ChatPage = state;

  /* ── 检索路 ── */
  function enabledPaths() {
    return qsa('#paths-selector .path-toggle')
      .filter(t => t.classList.contains('on'))
      .map(t => t.dataset.path);
  }

  function bindPaths() {
    qsa('#paths-selector .path-toggle').forEach(t => {
      t.addEventListener('click', e => {
        e.preventDefault();
        t.classList.toggle('on');
        const cb = t.querySelector('input');
        if (cb) cb.checked = t.classList.contains('on');
      });
    });
  }

  /* ── 会话 ── */
  function setSession(sessionId, title) {
    state.sessionId = sessionId || '';
    const url = new URL(location.href);
    if (sessionId) url.searchParams.set('session', sessionId);
    else url.searchParams.delete('session');
    history.replaceState(null, '', url);
    const t = qs('#chat-title');
    if (t && title) t.textContent = title;
  }

  async function loadSession(sessionId) {
    setSession(sessionId);
    qs('#chat-title').textContent = '加载中…';
    await Promise.all([
      Partials.refreshPiece('messages', { query: { session: sessionId } }),
      Partials.refreshPiece('ephemeral_docs', { query: { session: sessionId } }),
      Partials.refreshPiece('sessions', { query: { active: sessionId } }),
    ]);
    ChatUI.hydrateServerMessages(qs('#messages-container'));
    ChatUI.scrollToBottom();
    const item = qs(`.session-item[data-session-id="${CSS.escape(sessionId)}"] .session-title`);
    qs('#chat-title').textContent = item ? item.textContent : '会话';
  }

  function newSession() {
    setSession('');
    qs('#chat-title').textContent = '新对话';
    qs('#messages-container').innerHTML = '';
    qs('#ep-docs-bar').innerHTML = '';
    Partials.refreshPiece('sessions', { query: {} });
  }

  async function deleteSession(sessionId) {
    const ok = await confirmDialog({ title: '删除会话', message: '删除该会话及其临时文档？', confirmText: '删除' });
    if (!ok) return;
    try {
      await API.delete('/api/sessions/' + encodeURIComponent(sessionId));
      showToast('会话已删除', 'success');
      if (state.sessionId === sessionId) newSession();
      else Partials.refreshPiece('sessions', { query: { active: state.sessionId } });
    } catch (e) { showToast('删除失败：' + e.message, 'error'); }
  }

  function bindSessions(root) {
    qsa('.session-item', root).forEach(item => {
      item.addEventListener('click', e => {
        if (e.target.closest('[data-del-session]')) return;
        loadSession(item.dataset.sessionId);
      });
    });
    qsa('[data-del-session]', root).forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        deleteSession(btn.dataset.delSession);
      });
    });
  }

  /* ── 发送 ── */
  async function send() {
    const input = qs('#chat-input');
    const question = (input.value || '').trim();
    if (!question || state.sending) return;
    state.sending = true;
    const btn = qs('#btn-send');
    btn.classList.add('sending');
    btn.textContent = '生成中…';
    input.value = '';
    autoGrow(input);

    ChatUI.addUserMessage(question);
    ChatUI.showTyping();

    const payload = {
      question,
      session_id: state.sessionId || undefined,
      collections: undefined,
      retrieval_paths: enabledPaths(),
    };

    let shell = null;
    await SSEClient.stream('/api/chat', payload, {
      onEvent(type, data) {
        if (type === 'sources') {
          ChatUI.hideTyping();
          if (!shell) shell = ChatUI.addAssistantShell();
          shell.sources = data.sources || data.items || [];
        } else if (type === 'token') {
          ChatUI.hideTyping();
          if (!shell) shell = ChatUI.addAssistantShell();
          shell.appendToken(data.text || data.token || data.delta || '');
        } else if (type === 'done') {
          ChatUI.hideTyping();
          if (!shell) shell = ChatUI.addAssistantShell();
          shell.finalize(data.message_id);
          if (data.session_id && !state.sessionId) setSession(data.session_id);
          Partials.refreshPiece('sessions', { query: { active: state.sessionId } });
        } else if (type === 'error') {
          ChatUI.hideTyping();
          if (shell) shell.fail(data.message || '未知错误');
          else ChatUI.showError(data.message || '生成失败');
        }
      },
      onError(err) {
        ChatUI.hideTyping();
        if (shell) shell.fail(err.message || '连接失败');
        else ChatUI.showError(err.message || '连接失败');
      },
      onEnd() {
        ChatUI.hideTyping();
        state.sending = false;
        btn.classList.remove('sending');
        btn.textContent = '发送';
        if (shell && shell.block.classList.contains('streaming')) shell.finalize();
      },
    });
    state.sending = false;
    btn.classList.remove('sending');
    btn.textContent = '发送';
  }

  function autoGrow(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
  }

  /* ── 临时文档 ── */
  function bindAttach() {
    qs('#btn-attach').addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.pdf,.docx,.txt,.md,.csv,.png,.jpg,.jpeg';
      input.addEventListener('change', async () => {
        const file = input.files && input.files[0];
        if (!file) return;
        if (!state.sessionId) {
          try {
            const r = await API.post('/api/sessions', { title: '新对话' });
            setSession(r.session_id);
          } catch (e) { showToast('创建会话失败：' + e.message, 'error'); return; }
        }
        const fd = new FormData();
        fd.append('file', file);
        try {
          await API.upload('/api/sessions/' + encodeURIComponent(state.sessionId) + '/ephemeral', fd);
          showToast('临时文档已上传', 'success');
          Partials.refreshPiece('ephemeral_docs', { query: { session: state.sessionId } });
        } catch (e) { showToast('上传失败：' + e.message, 'error'); }
      });
      input.click();
    });

    document.addEventListener('click', async e => {
      const btn = e.target.closest && e.target.closest('[data-ep-remove]');
      if (!btn) return;
      try {
        await API.delete('/api/sessions/' + encodeURIComponent(state.sessionId) +
          '/ephemeral/' + encodeURIComponent(btn.dataset.epRemove));
        Partials.refreshPiece('ephemeral_docs', { query: { session: state.sessionId } });
      } catch (err) { showToast('移除失败：' + err.message, 'error'); }
    });
  }

  /* ── 初始化 ── */
  function init() {
    const page = qs('#chat-page');
    if (!page) return;
    state.sessionId = page.dataset.sessionId || '';

    bindPaths();
    bindSessions(document);
    bindAttach();
    ChatUI.hydrateServerMessages(document);
    ChatUI.scrollToBottom();

    Partials.registerRebinder('sessions', bindSessions);
    Partials.registerRebinder('messages', root => ChatUI.hydrateServerMessages(root));

    qs('#btn-new-session').addEventListener('click', newSession);
    const input = qs('#chat-input');
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    input.addEventListener('input', () => autoGrow(input));
    qs('#btn-send').addEventListener('click', send);
    qs('#chat-error-close').addEventListener('click', () => qs('#chat-error').classList.add('hidden'));
    qsa('.hint-chip').forEach(c => c.addEventListener('click', () => {
      input.value = c.textContent;
      input.focus();
      autoGrow(input);
    }));

    if (state.sessionId) {
      loadSession(state.sessionId);
    } else {
      Partials.refreshPiece('sessions', { query: {} });
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
