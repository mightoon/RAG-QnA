/* 知识库上传：选择/拖拽 → 暂存列表 → 集合与角色 → 上传 → 批次进度 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const showToast = (msg, type) => {
    if (window.Partials && window.Partials.showToast) window.Partials.showToast(msg, type);
    else console.log('[toast]', type, msg);
  };
  // 必须与后端 SUPPORTED_EXTS（rag/api/routes/documents.py）一致：
  // 前端列表更窄时，用户选了后端其实支持的文件也会被自己挡掉（.pptx/.xlsx/
  // .html/.tiff 等以前都不在列表里，表现为"上传按钮点不动/文件选不了"）
  const ACCEPT = ['.pdf', '.docx', '.doc', '.xlsx', '.xls', '.csv', '.pptx',
    '.ppt', '.md', '.markdown', '.txt', '.log', '.html', '.htm',
    '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp'];

  let ctx = null;          // { refreshAll, state }
  let staging = [];        // File[]
  let rolesSelected = [];
  // 内容已在库里的文件（后端按 MD5 判定、未写任何库），等用户决定怎么处理
  let pendingDuplicates = [];   // [{ token, items: [duplicate...] }]

  /* ── 重复文件确认：覆盖重跑 / 作为新文档 / 跳过 ─────────────────
     为什么必须问：同一份内容再传一次，用户要的通常是"按当前口径重跑一遍"，
     而秒传会静默复用旧结果（界面上是"一瞬间完成"）。把决定权交回用户，
     并把三个后果写清楚 —— 尤其"作为新文档"会造成同内容两套块。 */
  function askDuplicate(dup) {
    return new Promise(resolve => {
      const wrap = document.createElement('div');
      const info = document.createElement('div');
      info.className = 'modal-warning-box';
      info.innerHTML = '检测到该文件的<b>内容已经入库过</b>（按内容 MD5 判定，'
        + '与文件名、作者、修改时间等属性无关）。本次上传<b>尚未写入任何库</b>，'
        + '下面选完才会真正入库。';
      wrap.appendChild(info);

      const table = document.createElement('table');
      table.className = 'data-table';
      table.style.marginTop = '10px';
      table.innerHTML =
        '<tbody>'
        + `<tr><td style="width:110px">本次文件</td><td><b>${esc(dup.filename)}</b></td></tr>`
        + `<tr><td>已存在文档</td><td>${esc(dup.existing_filename || dup.doc_id)}`
        + `（${dup.doc_id}）</td></tr>`
        + `<tr><td>入库时间</td><td>${esc((dup.uploaded_at || '').replace('T', ' ').slice(0, 19))}`
        + `　·　v${dup.version || 1}</td></tr>`
        + `<tr><td>已有内容</td><td>${dup.chunk_count || 0} 块 / ${dup.page_count || 0} 页</td></tr>`
        + '</tbody>';
      wrap.appendChild(table);

      const hint = document.createElement('p');
      hint.className = 'modal-text';
      hint.style.marginTop = '10px';
      hint.textContent = (dup.engine_changed
        ? '⚠ 当前解析口径与上次入库时不同（换过引擎/参数或代码已升级）：'
          + '「重解析」才会按新口径更新索引。'
        : (dup.same_roles ? '' : '⚠ 本次的可见角色与已存在文档不同。'));
      if (hint.textContent) wrap.appendChild(hint);

      const opts = [
        ['reingest', '重解析（覆盖原文档）',
          '沿用原文档，重跑解析链并替换旧块 —— 不产生重复内容，推荐'],
        ['new', '作为新文档入库',
          '生成一篇新文档；同内容会存在两套块（检索可能重复命中）'],
        ['skip', '跳过这个文件', '什么都不做，丢弃本次上传的这份'],
      ];
      const group = document.createElement('div');
      group.style.marginTop = '12px';
      opts.forEach(([val, label, desc], i) => {
        const row = document.createElement('label');
        row.style.display = 'block';
        row.style.marginBottom = '8px';
        row.style.cursor = 'pointer';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'dup-action';
        radio.value = val;
        radio.checked = i === 0;
        row.appendChild(radio);
        const t = document.createElement('b');
        t.textContent = ' ' + label;
        row.appendChild(t);
        const d = document.createElement('div');
        d.className = 'doc-title-sub';
        d.style.marginLeft = '22px';
        d.textContent = desc;
        row.appendChild(d);
        group.appendChild(row);
      });
      wrap.appendChild(group);

      const footer = document.createElement('div');
      footer.className = 'modal-footer';
      const cancel = document.createElement('button');
      cancel.className = 'btn btn-secondary';
      cancel.textContent = '取消本次上传';
      const ok = document.createElement('button');
      ok.className = 'btn btn-primary';
      ok.textContent = '确认';
      footer.appendChild(cancel);
      footer.appendChild(ok);

      // ⚠ 只允许 settle 一次：模态框的 close() 会回调 onClose（见 partials.js），
      // 而按钮处理函数里也要 close()。若 onClose 里先 resolve('skip')，按钮那句
      // resolve(选中项) 就成了空操作 —— Promise 只认第一次 settle。
      // 实测症状：无论选「重解析」还是「作为新文档」，后端收到的都是 skip，
      // 于是"确认后什么都没发生、任务秒完成"。用 settled 标志把两者分开。
      let settled = false;
      const decide = (v) => { if (!settled) { settled = true; resolve(v); } };
      const m = window.Partials.modal({
        title: '该文件已上传过，是否再次入库？',
        subtitle: '按内容 MD5 判定为同一文件',
        body: wrap, footer: footer, type: 'warning',
        onClose: () => decide('skip'),        // 点 ✕ / Esc / 点遮罩关闭 = 跳过
      });
      if (!m) { decide('skip'); return; }
      cancel.addEventListener('click', () => { decide('cancel'); m.close(); });
      ok.addEventListener('click', () => {
        const picked = wrap.querySelector('input[name="dup-action"]:checked');
        decide(picked ? picked.value : 'skip');
        m.close();
      });
    });
  }

  function esc(s) {
    return window.utils && window.utils.escapeHtml
      ? window.utils.escapeHtml(String(s == null ? '' : s))
      : String(s == null ? '' : s);
  }

  async function resolvePendingDuplicates() {
    const queues = pendingDuplicates;
    pendingDuplicates = [];
    for (const q of queues) {
      const decisions = {};
      let cancelled = false;
      for (const dup of q.items) {
        const action = await askDuplicate(dup);
        if (action === 'cancel') { cancelled = true; break; }
        decisions[dup.md5] = action;
      }
      const api = (window.API && API.post) ? API.post : null;
      try {
        if (cancelled) {
          if (api) await api('/api/documents/upload/discard',
                             { staging_token: q.token });          showToast('已取消本次上传（未写入任何库）', 'info');
          continue;
        }
        const res = await api('/api/documents/upload/confirm',
                              { staging_token: q.token, decisions: decisions });
        const n = (res.tasks || []).length;
        showToast(n
          ? `已按你的选择入队 ${n} 个任务`
            + (res.skipped ? `，跳过 ${res.skipped} 个` : '')
          : `没有任务入队（跳过 ${res.skipped || 0} 个）`,
          n ? 'success' : 'info');
        if (ctx && n) { ctx.state.tasks.loaded = false; ctx.refreshAll(); }
      } catch (e) {
        showToast('确认失败：' + e.message + '（可在文档列表用「重解析」重试）',
                  'error');
      }
    }
  }


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
    const tooBig = [];
    let added = 0;
    list.forEach(f => {
      // 文件夹上传时 File.name 仍是**文件名**（目录信息在 webkitRelativePath 里），
      // 这里按 name 取扩展名即可；后端把 filename 直接拼进对象存储 key
      // （{tenant}/{doc_id}/{filename}），所以绝不能把相对路径当 filename 送过去。
      const name = baseName(f.name);
      const ext = '.' + (name.split('.').pop() || '').toLowerCase();
      if (!ACCEPT.includes(ext)) { bad.push(name); return; }
      // 单个文件就超过整批上限的：分批也救不了（它自己就是一批），在这里拦住
      // 并说清原因，比让它进列表后在上传时收一个整批 400 更好懂。
      if (f.size > BATCH_BYTES) { tooBig.push(name); return; }
      if (staging.some(x => x.name === name && x.size === f.size)) return;
      staging.push(f);
      added++;
    });
    if (tooBig.length) {
      const sample = tooBig.slice(0, 3).join('、');
      showToast(`已跳过 ${tooBig.length} 个超过单批上限（${fmtSize(BATCH_BYTES)}）`
        + `的文件（${sample}${tooBig.length > 3 ? ' 等' : ''}）`, 'warning');
    }
    // 目录批量上传动辄几百个文件，绝大多数是无关格式（.DS_Store/.git 等）：
    // 逐个弹提示会刷屏，这里只报数量 + 前几个样例
    if (bad.length) {
      const sample = bad.slice(0, 5).join('、');
      showToast(`已跳过 ${bad.length} 个不支持的文件（${sample}`
        + (bad.length > 5 ? ' 等' : '') + '）', 'warning');
    }
    if (!added && !bad.length && !tooBig.length) {
      showToast('没有新增文件（重复或已在上传列表里）', 'info');
    }
    renderStaging();
  }

  function baseName(path) {
    const s = String(path || '');
    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i >= 0 ? s.slice(i + 1) : s;
  }

  // ── 目录批量上传 ────────────────────────────────────────
  // 两个入口：①「上传文件夹」按钮（webkitdirectory）②把文件夹拖进来。
  // 拖文件夹以前是**静默失败**：dataTransfer.files 里那一条是"目录"本身，
  // 没有扩展名 → 被当成不支持格式丢掉，用户看到"不支持的文件"却不知道是为什么。

  function pickFolder() {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.webkitdirectory = true;   // 非标准属性，Chrome/Edge/Safari/Firefox 均支持
    input.style.display = 'none';
    let done = false;
    input.addEventListener('change', () => {
      done = true;
      addFiles(input.files);
      input.remove();
    });
    // 用户点"取消"时 change 不触发 → 兜底清掉挂在 body 上的 input
    window.addEventListener('focus', () => setTimeout(() => {
      if (!done) input.remove();
    }, 600), { once: true });
    document.body.appendChild(input);
    input.click();
  }

  // 拖拽进来的条目可能含目录：用 webkitGetAsEntry 递归展开
  function entriesFromDrop(dt) {
    const items = dt && dt.items;
    if (!items || !items.length || !items[0].webkitGetAsEntry) {
      return Promise.resolve(dt ? Array.from(dt.files || []) : []);
    }
    const roots = [];
    for (let i = 0; i < items.length; i++) {
      const entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
      if (entry) roots.push(entry);
    }
    if (!roots.length) return Promise.resolve(Array.from(dt.files || []));
    return Promise.all(roots.map(readEntry)).then(flat => flat.flat(Infinity));
  }

  function readEntry(entry) {
    return new Promise(resolve => {
      if (!entry) return resolve([]);
      if (entry.isFile) {
        entry.file(f => resolve([f]), () => resolve([]));
        return;
      }
      if (!entry.isDirectory) return resolve([]);
      const reader = entry.createReader();
      const out = [];
      const readBatch = () => reader.readEntries(async entries => {
        if (!entries.length) return resolve(out);
        const nested = await Promise.all(entries.map(readEntry));
        nested.forEach(n => out.push(...n));
        readBatch();          // readEntries 一次最多 100 条，要反复读到空
      }, () => resolve(out));
      readBatch();
    });
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
    const batches = chunkStaging();
    let uploaded = 0;
    let dedupCount = 0;
    if (btn) btn.disabled = true;
    try {
      for (let i = 0; i < batches.length; i++) {
        const files = batches[i];
        if (btn) btn.textContent = `上传中… ${i + 1}/${batches.length}`;
        const fd = new FormData();
        files.forEach(f => fd.append('files', f));
        fd.append('collection', currentCollection());
        // 后端按 JSON 解析 allowed_roles（json.loads）——发裸字符串 "a,b" 会让它
        // 抛 JSONDecodeError → 整个上传 500。这里如实发 JSON 数组。
        if (rolesSelected.length) fd.append('allowed_roles', JSON.stringify(rolesSelected));
        const res = await API.upload('/api/documents/upload', fd);
        // 只统计**真正入队**的任务：待确认的重复文件也在 total 里，但什么都没写
        uploaded += (res.tasks ? res.tasks.length : (res.total || files.length));
        // 秒传（MD5 去重）的文件**没有被解析、也没有写任何库**，只是复用已有索引。
        // 不说清楚的话，"一瞬间完成"看起来就像"按新代码重解析过了"。
        const deduped = (res.tasks || []).filter(t => t.dedup).length;
        if (deduped) dedupCount += deduped;
        // 内容已在库里的文件：后端**什么都没写**，等用户决定（见 confirmDuplicates）
        if (res.duplicates && res.duplicates.length && res.staging_token) {
          pendingDuplicates.push({ token: res.staging_token, items: res.duplicates });
        }
        // 成功的这批从暂存里摘掉：中途失败时留在列表里可以重试，不会重复入队
        files.forEach(f => {
          const j = staging.indexOf(f);
          if (j >= 0) staging.splice(j, 1);
        });
        renderStaging();
      }
      const base = batches.length > 1
        ? `已入队 ${uploaded} 个任务（分 ${batches.length} 批）`
        : `已入队 ${uploaded} 个任务`;
      showToast(dedupCount
        ? `${base}；其中 ${dedupCount} 个是已入库过的同一文件（秒传：未重新解析、`
          + `未写入任何库，要按当前口径重跑请到文档列表点「重解析」）`
        : base, dedupCount ? 'warning' : 'success');
      if (ctx) {
        ctx.state.tasks.loaded = false;
        ctx.refreshAll();
        if (window.Knowledge) Knowledge.switchTab('tasks');
      }
      // 有待确认的重复文件 → 逐个弹框问清楚再决定（不弹就是替用户做主了）
      await resolvePendingDuplicates();
    } catch (e) {
      // 说清楚"已经进去了多少"：否则用户不知道要不要重传
      showToast(`上传失败（已入队 ${uploaded} 个）：${e.message}`
        + (staging.length ? '，未上传的仍在列表中' : ''), 'error');
    } finally {
      if (btn) { btn.disabled = false; }
      renderStaging();
    }
  }

  // 单批的文件数与字节上限：后端按**整批**校验 max_upload_mb（超限直接 400 丢整批），
  // 目录上传动辄几百个文件必然踩到它 —— 所以前端主动分批，每批独立入队、互不牵连。
  const BATCH_FILES = 50;
  // 字节上限必须跟后端 max_upload_mb 一致（模板注入 __MAX_UPLOAD_MB__）：写死
  // 100MB 而运维把上限调到 50MB 时，每批都会 400。留 10% 余量防 multipart
  // 边界/表单字段把总量顶过上限。
  const BATCH_BYTES = (function () {
    const mb = Number(window.__MAX_UPLOAD_MB__) || 0;
    if (mb > 0) return Math.max(1, Math.floor(mb * 0.9)) * 1024 * 1024;
    return 100 * 1024 * 1024;   // 没注入时退回保守值（后端默认 500MB）
  })();

  function chunkStaging() {
    const out = [];
    let cur = [];
    let bytes = 0;
    staging.forEach(f => {
      if (cur.length && (cur.length >= BATCH_FILES
          || bytes + f.size > BATCH_BYTES)) {
        out.push(cur);
        cur = [];
        bytes = 0;
      }
      cur.push(f);
      bytes += f.size;
    });
    if (cur.length) out.push(cur);
    return out;
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
    // 目录批量上传（按钮少的时候不显示：老模板没有这个元素也能跑）
    const btnDir = qs('#btn-upload-dir');
    if (btnDir) btnDir.addEventListener('click', pickFolder);

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
        if (!e.dataTransfer) return;
        // 拖进来的可能是**文件夹**：dataTransfer.files 里那条是目录本身（没有
        // 扩展名 → 以前会被当成"不支持格式"静默丢掉），必须走 entry 递归展开
        entriesFromDrop(e.dataTransfer).then(files => addFiles(files));
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

  window.KnowledgeUpload = { init, addFiles, updateQueueHint, askDuplicate };
})();
