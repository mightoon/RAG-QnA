/* 知识库上传：选择/拖拽 → 暂存列表 → 集合与角色 → 上传 → 批次进度 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const showToast = (msg, type) => {
    if (window.Partials && window.Partials.showToast) window.Partials.showToast(msg, type);
    else console.log('[toast]', type, msg);
  };
  const ACCEPT = ['.pdf', '.docx', '.doc', '.txt', '.md', '.csv', '.png', '.jpg', '.jpeg'];

  let ctx = null;          // { refreshAll, state }
  let staging = [];        // File[]
  let rolesSelected = [];

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }

  function renderStaging() {
    const box = qs('#staging-list');
    if (!box) return;
    box.innerHTML = '';
    staging.forEach((f, i) => {
      const item = document.createElement('div');
      item.className = 'staging-item';
      item.innerHTML = '<span class="st-name"></span><span class="st-meta"></span>';
      item.querySelector('.st-name').textContent = f.name;
      item.querySelector('.st-meta').textContent = fmtSize(f.size);
      const del = document.createElement('button');
      del.className = 'btn btn-ghost btn-xs';
      del.textContent = '移除';
      del.addEventListener('click', () => { staging.splice(i, 1); renderStaging(); });
      item.appendChild(del);
      box.appendChild(item);
    });
    const rf = qs('#roles-field');
    if (rf) rf.classList.toggle('hidden', !staging.length);
    const btn = qs('#btn-upload');
    if (btn) btn.textContent = staging.length ? `上传 ${staging.length} 个文档` : '上传文档';
  }

  function addFiles(files) {
    const list = Array.from(files || []);
    const bad = [];
    list.forEach(f => {
      const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
      if (!ACCEPT.includes(ext)) { bad.push(f.name); return; }
      if (staging.some(x => x.name === f.name && x.size === f.size)) return;
      staging.push(f);
    });
    if (bad.length) showToast('不支持的格式：' + bad.join('、'), 'warning');
    renderStaging();
  }

  function currentCollection() {
    const sel = qs('#doc-collection-filter');
    return (sel && sel.value) || (window.__COLLECTIONS__[0] && window.__COLLECTIONS__[0].name) || 'default';
  }

  async function doUpload() {
    if (!staging.length) { pickFile(); return; }
    const queue = (ctx && ctx.state.queue) || {};
    if (queue.queueFull) {
      showToast('任务队列已满，请稍后重试', 'warning');
      return;
    }
    const btn = qs('#btn-upload');
    if (btn) { btn.disabled = true; btn.textContent = '上传中…'; }
    try {
      const fd = new FormData();
      staging.forEach(f => fd.append('files', f));
      fd.append('collection', currentCollection());
      if (rolesSelected.length) fd.append('allowed_roles', rolesSelected.join(','));
      const res = await API.upload('/api/ingest/upload', fd);
      showToast(`已入队 ${res.total || staging.length} 个任务`, 'success');
      staging = [];
      renderStaging();
      if (ctx) {
        ctx.state.tasks.loaded = false;
        ctx.refreshAll();
        if (window.Knowledge) Knowledge.switchTab('tasks');
      }
    } catch (e) {
      showToast('上传失败：' + e.message, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '上传文档'; }
    }
  }

  function pickFile() {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = ACCEPT.join(',');
    input.addEventListener('change', () => addFiles(input.files));
    input.click();
  }

  function updateQueueHint() {
    const hint = qs('#queue-hint');
    if (!hint || !ctx) return;
    const q = ctx.state.queue || {};
    hint.textContent = q.queueDepthLimit
      ? `队列 ${q.queuedTasks || 0}/${q.queueDepthLimit}`
      : '';
    hint.style.color = q.queueFull ? 'var(--error)' : '';
  }

  function init(context) {
    ctx = context || {};
    const btn = qs('#btn-upload');
    if (btn) btn.addEventListener('click', doUpload);

    const dz = qs('#drop-zone');
    const page = qs('#knowledge-page');
    if (page && dz) {
      ['dragenter', 'dragover'].forEach(ev => page.addEventListener(ev, e => {
        e.preventDefault();
        dz.classList.remove('hidden');
        dz.classList.add('drag-over');
      }));
      ['dragleave', 'drop'].forEach(ev => page.addEventListener(ev, e => {
        e.preventDefault();
        if (ev === 'dragleave' && e.target !== page) return;
        dz.classList.add('hidden');
        dz.classList.remove('drag-over');
      }));
      page.addEventListener('drop', e => {
        if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
      });
    }

    // 角色选择（可选，来自 perm 配置）
    const roles = (window.__ROLES__ || []);
    const rf = qs('#roles-field');
    if (rf && roles.length) {
      roles.forEach(r => {
        const chip = document.createElement('label');
        chip.className = 'path-toggle';
        chip.innerHTML = '<input type="checkbox" style="display:none"><span></span>';
        chip.querySelector('span').textContent = r;
        chip.addEventListener('click', e => {
          e.preventDefault();
          const on = chip.classList.toggle('on');
          if (on) rolesSelected.push(r);
          else rolesSelected = rolesSelected.filter(x => x !== r);
        });
        rf.appendChild(chip);
      });
    } else if (rf) {
      rf.remove();
    }

    updateQueueHint();
  }

  window.KnowledgeUpload = { init, addFiles, updateQueueHint };
})();
