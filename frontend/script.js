// ─── Config ───────────────────────────────────────────────────────────────────
const API = 'http://localhost:8000/api';

// ─── State ────────────────────────────────────────────────────────────────────
let currentConvId   = null;
let isLoginMode     = true;
let notesFile       = null;
let appInited       = false;
let _pipelineDone   = false;

// ─── Theme ────────────────────────────────────────────────────────────────────
function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-icon').className = isLight ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
}
function initTheme() {
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light');
    const icon = document.getElementById('theme-icon');
    if (icon) icon.className = 'fa-solid fa-sun';
  }
}

// ─── Auth helpers ─────────────────────────────────────────────────────────────
const getToken      = ()     => localStorage.getItem('access_token');
const getRefreshTok = ()     => localStorage.getItem('refresh_token');
const setTokens     = (a, r) => { localStorage.setItem('access_token', a); localStorage.setItem('refresh_token', r); };
const clearTokens   = ()     => { localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); };
function authHeaders() { return { 'Content-Type': 'application/json', 'Authorization': `Bearer ${getToken()}` }; }

let _refreshProm = null;
async function refreshToken() {
  if (_refreshProm) return _refreshProm;
  _refreshProm = (async () => {
    const rt = getRefreshTok();
    if (!rt) throw new Error('No refresh token');
    const r = await fetch(`${API}/auth/refresh`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!r.ok) throw new Error('Refresh failed');
    const d = await r.json();
    setTokens(d.access_token, d.refresh_token);
  })();
  try { return await _refreshProm; } finally { _refreshProm = null; }
}

async function apiFetch(path, opts = {}) {
  let r = await fetch(`${API}${path}`, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (r.status === 401) {
    try {
      await refreshToken();
      r = await fetch(`${API}${path}`, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
    } catch { clearTokens(); showAuth(); throw new Error('Session expired'); }
  }
  return r;
}

// ─── Auth UI ──────────────────────────────────────────────────────────────────
function showAuth() {
  document.getElementById('auth-modal').classList.remove('hidden');
  document.getElementById('main-ui').classList.add('hidden');
  appInited = false;
}
function hideAuth() {
  document.getElementById('auth-modal').classList.add('hidden');
  document.getElementById('main-ui').classList.remove('hidden');
}
function toggleAuthMode() {
  isLoginMode = !isLoginMode;
  document.getElementById('auth-title').textContent       = isLoginMode ? 'Sign in' : 'Create account';
  document.getElementById('auth-sub').textContent         = isLoginMode ? 'Welcome back — let\'s ship something great.' : 'Join BacklogAI and streamline your sprints.';
  document.getElementById('auth-submit').textContent      = isLoginMode ? 'Sign in' : 'Register';
  document.getElementById('auth-toggle-text').textContent = isLoginMode ? 'No account? Register free' : 'Already have an account? Sign in';
  document.getElementById('auth-error').textContent       = '';
}
async function submitAuth() {
  const email = document.getElementById('auth-email').value.trim();
  const pwd   = document.getElementById('auth-password').value;
  const errEl = document.getElementById('auth-error');
  const btn   = document.getElementById('auth-submit');
  errEl.textContent = ''; btn.disabled = true; btn.textContent = 'Please wait…';
  try {
    let res;
    if (isLoginMode) {
      res = await fetch(`${API}/auth/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ username: email, password: pwd }),
      });
    } else {
      res = await fetch(`${API}/auth/register`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password: pwd }),
      });
    }
    const data = await res.json();
    if (!res.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(', ') : (data.detail || `HTTP ${res.status}`));
    setTokens(data.access_token, data.refresh_token);
    setUserDisplay(data.user.email);
    hideAuth(); await init();
  } catch (e) { errEl.textContent = e.message; }
  finally { btn.disabled = false; btn.textContent = isLoginMode ? 'Sign in' : 'Register'; }
}
document.getElementById('auth-email').addEventListener('keydown',    e => { if (e.key === 'Enter') submitAuth(); });
document.getElementById('auth-password').addEventListener('keydown', e => { if (e.key === 'Enter') submitAuth(); });

function setUserDisplay(email) {
  document.getElementById('user-email-display').textContent = email;
  document.getElementById('user-avatar').textContent        = email[0].toUpperCase();
}
async function doLogout() {
  try { await apiFetch('/auth/logout', { method: 'POST', body: JSON.stringify({ refresh_token: getRefreshTok() }) }); } catch {}
  clearTokens(); currentConvId = null; appInited = false; _pipelineDone = false;
  document.getElementById('chat-box').innerHTML = '';
  document.getElementById('conversations-list').innerHTML = '';
  showAuth();
}

// ─── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  if (appInited) return; appInited = true; initTheme();
  const convs = await fetchConversations();
  if (convs.length === 0) await createNewConversation();
  else { renderSidebar(convs); await switchConversation(convs[0].id); }
}

// ─── Conversations ────────────────────────────────────────────────────────────
async function fetchConversations() {
  try { return (await (await apiFetch('/conversations')).json()).conversations || []; } catch { return []; }
}
async function createNewConversation() {
  const conv = await (await apiFetch('/conversations', { method: 'POST', body: JSON.stringify({ title: 'New Session' }) })).json();
  currentConvId = conv.id; _pipelineDone = false;
  document.getElementById('chat-box').innerHTML = '';
  document.getElementById('session-title-display').textContent = conv.title;
  document.getElementById('download-btn').classList.add('hidden');
  showPanel('upload');
  await loadConversations();
}
async function loadConversations() { renderSidebar(await fetchConversations()); }
function renderSidebar(convs) {
  const list = document.getElementById('conversations-list');
  list.innerHTML = '';
  convs.forEach(conv => {
    const el = document.createElement('div');
    el.className = 'conv-item' + (conv.id === currentConvId ? ' active' : '');
    el.innerHTML = `
      <span class="conv-icon">📋</span>
      <div class="conv-body">
        <div class="conv-title">${esc(conv.title)}</div>
        <div class="conv-meta">${conv.message_count} msgs · ${relTime(conv.updated_at)}</div>
      </div>
      <button class="conv-delete" onclick="event.stopPropagation();deleteConv('${conv.id}')">
        <i class="fa-solid fa-trash"></i>
      </button>`;
    el.onclick = () => switchConversation(conv.id);
    list.appendChild(el);
  });
}
async function switchConversation(id) {
  currentConvId = id; _pipelineDone = false;
  document.getElementById('chat-box').innerHTML = '';
  await loadMessages(id); await loadConversations();
}
async function loadMessages(id) {
  const data = await (await apiFetch(`/conversations/${id}/messages`)).json();
  const b    = chatBox(); b.innerHTML = '';
  (data.messages || []).forEach(m => {
    if (['meeting_notes','review_decisions','general_query','pinecone_snapshot'].includes(m.msg_type)) return;
    appendMessage(m.content, m.role, m.msg_type, false);
  });
  b.scrollTop = b.scrollHeight;

  const status = await (await apiFetch(`/sessions/${id}/status`)).json();
  document.getElementById('session-title-display').textContent = id.slice(0, 8) + '…';

  if (status.stage === 'done') {
    _pipelineDone = true;
    forceDonePanel();
  } else if (status.stage === 'review') {
    _pipelineDone = false;
    showPanel('review');
  } else {
    _pipelineDone = false;
    showPanel('upload');
    document.getElementById('download-btn').classList.add('hidden');
  }
}
async function deleteConv(id) {
  if (!confirm('Delete this session?')) return;
  await apiFetch(`/conversations/${id}`, { method: 'DELETE' });
  if (id === currentConvId) { currentConvId = null; _pipelineDone = false; await createNewConversation(); }
  else await loadConversations();
}

// ─── Panel management ─────────────────────────────────────────────────────────
function showPanel(name) {
  document.getElementById('upload-panel').classList.toggle('hidden',   name !== 'upload');
  document.getElementById('processing-bar').classList.toggle('hidden', name !== 'processing');
  document.getElementById('review-panel').classList.toggle('hidden',   name !== 'review');
  document.getElementById('chat-input-bar').classList.toggle('hidden',
    name !== 'chat' && name !== 'done' && name !== 'review');
}

// forceDonePanel: explicitly shows the done state — belt-AND-suspenders approach.
// Uses both showPanel AND direct DOM manipulation so there's no ambiguity.
function forceDonePanel() {
  // Hide everything except chat input
  document.getElementById('upload-panel').classList.add('hidden');
  document.getElementById('processing-bar').classList.add('hidden');
  document.getElementById('review-panel').classList.add('hidden');
  // Force chat-input-bar visible — this is the critical line
  document.getElementById('chat-input-bar').classList.remove('hidden');
  document.getElementById('download-btn').classList.remove('hidden');
  // Focus the input so user can type immediately
  const inp = document.getElementById('user-input');
  if (inp) inp.focus();
}

// restorePanel: after a stream ends, show correct panel based on _pipelineDone
function restorePanel() {
  if (_pipelineDone) forceDonePanel();
  else showPanel('upload');
}

// ─── File handlers ────────────────────────────────────────────────────────────
function handleNotesFile(e) {
  const file = e.target.files[0]; if (!file) return;
  notesFile = file;
  document.getElementById('attachment-chip').classList.remove('hidden');
  document.getElementById('attachment-name').textContent = file.name;
  document.getElementById('notes-textarea').value = '';
  autoResizeComposer(document.getElementById('notes-textarea'));
}
function removeAttachment() {
  notesFile = null;
  document.getElementById('attachment-chip').classList.add('hidden');
  document.getElementById('attachment-name').textContent = '';
  document.getElementById('notes-file-input').value = '';
}
function autoResizeComposer(el) {
  el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}
function composerKeydown(e) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runPipeline(); }
}

// ─── Run pipeline (from upload panel) ─────────────────────────────────────────
async function runPipeline() {
  const errEl    = document.getElementById('upload-error');
  const textArea = document.getElementById('notes-textarea').value.trim();
  errEl.textContent = ''; errEl.classList.remove('visible');
  if (!notesFile && !textArea) {
    errEl.textContent = '⚠ Attach a file or paste your input first.'; errEl.classList.add('visible'); return;
  }
  const form = new FormData();
  if (notesFile) form.append('meeting_notes_file', notesFile);
  if (textArea)  form.append('meeting_notes_text', textArea);
  if (notesFile) appendMessage(`📎 **${notesFile.name}**`, 'user', 'text', true);
  else appendMessage(textArea, 'user', 'text', true);
  document.getElementById('notes-textarea').value = '';
  autoResizeComposer(document.getElementById('notes-textarea'));
  removeAttachment();
  showPanel('processing');
  document.getElementById('processing-label').textContent = 'Analysing input…';
  try {
    const ur = await fetch(`${API}/sessions/${currentConvId}/upload`, {
      method: 'POST', headers: { 'Authorization': `Bearer ${getToken()}` }, body: form,
    });
    if (!ur.ok) {
      restorePanel(); appendMessage((await ur.json().catch(() => ({}))).detail || 'Upload failed', 'assistant', 'text', true); return;
    }
    const ud = await ur.json();
    if (ud.route === 'MEETING_NOTES') {
      appendMessage(`📄 **Meeting notes loaded** · ${(ud.chars||0).toLocaleString()} chars`, 'assistant', 'text', true);
      await streamPipeline();
    } else {
      await streamGeneralAnswer();
    }
  } catch (e) { restorePanel(); appendMessage(`❌ ${e.message}`, 'assistant', 'text', true); }
}

// ─── Stream pipeline ──────────────────────────────────────────────────────────
async function streamPipeline() {
  showPanel('processing');
  document.getElementById('processing-label').textContent = 'Agent is running…';
  const res = await fetch(`${API}/sessions/${currentConvId}/process/stream`, {
    method: 'POST', headers: { 'Authorization': `Bearer ${getToken()}` },
  });
  if (!res.ok) {
    appendMessage(`❌ ${(await res.json().catch(()=>({}))).detail || 'Pipeline failed'}`, 'assistant', 'text', true);
    restorePanel(); return;
  }
  await consumeSSE(res);
}

// ─── Stream general answer ────────────────────────────────────────────────────
async function streamGeneralAnswer() {
  showPanel('processing');
  document.getElementById('processing-label').textContent = 'Thinking…';
  const res = await fetch(`${API}/sessions/${currentConvId}/general/stream`, {
    method: 'POST', headers: { 'Authorization': `Bearer ${getToken()}` },
  });
  if (!res.ok) {
    appendMessage(`❌ ${(await res.json().catch(()=>({}))).detail || 'Search failed'}`, 'assistant', 'text', true);
    restorePanel(); return;
  }
  await consumeSSE(res);
}

// ─── Shared SSE reader ────────────────────────────────────────────────────────
async function consumeSSE(res) {
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try { onSSE(JSON.parse(line.slice(6))); } catch {}
      }
    }
  } catch (e) { console.warn('SSE interrupted:', e); restorePanel(); }
}

// ─── SSE event handler (synchronous — no await inside) ───────────────────────
let _currentReviewData = null;
function onSSE(ev) {
  switch (ev.type) {
    case 'step': {
      const lbl = document.getElementById('processing-label');
      if (lbl) lbl.textContent = ev.message;
      if (!document.getElementById(`step-${ev.step}`)) {
        const el = document.createElement('div');
        el.className = 'msg msg-system-step'; el.id = `step-${ev.step}`; el.textContent = ev.message;
        chatBox().appendChild(el); chatBox().scrollTop = chatBox().scrollHeight;
      }
      break;
    }
    case 'step_done': {
      const old = document.getElementById(`step-${ev.step}`); if (old) old.remove();
      appendMessage(ev.message, 'assistant', 'text', true); break;
    }
    case 'info':
    case 'warning':
      appendMessage(ev.message, 'assistant', 'text', true); break;

    case 'review_card': {
      _currentReviewData = ev.change_data || null;
      const el = document.createElement('div');
      el.className = 'msg msg-assistant'; el.innerHTML = renderMarkdown(ev.content);
      chatBox().appendChild(el); chatBox().scrollTop = chatBox().scrollHeight;
      showPanel('review'); break;
    }

    case 'answer':
      // Remove any orphaned step bubbles (e.g. "Searching the web…" when 0 searches ran)
      chatBox().querySelectorAll('.msg-system-step').forEach(el => el.remove());
      appendMessage(ev.message, 'assistant', 'text', true);
      restorePanel(); break;

    case 'done':
      // Pipeline complete — clean up any step indicators, mark done, force chat input visible
      chatBox().querySelectorAll('.msg-system-step').forEach(el => el.remove());
      appendMessage(ev.message, 'assistant', 'result', true);
      _pipelineDone = true;
      forceDonePanel(); break;

    case 'error':
      appendMessage(`❌ ${ev.message}`, 'assistant', 'text', true);
      restorePanel(); break;
  }
}

// ─── Review ───────────────────────────────────────────────────────────────────
async function sendReview(decision) {
  showLoading('Saving decision…');
  try {
    const r    = await apiFetch(`/sessions/${currentConvId}/review`, { method: 'POST', body: JSON.stringify({ decision }) });
    const data = await r.json();
    hideLoading();
    appendMessage({ APPROVE:'✅ Approved', REJECT:'❌ Rejected', EDIT:'✏️ Edited' }[decision], 'user', 'review_decision', true);

    if (data.status === 'next') {
      _currentReviewData = data.change_data || null;
      const el = document.createElement('div');
      el.className = 'msg msg-assistant'; el.innerHTML = renderMarkdown(data.review_card);
      chatBox().appendChild(el); chatBox().scrollTop = chatBox().scrollHeight;
      showPanel('review');

    } else if (data.status === 'done') {
      _pipelineDone = true;
      // Append the result message
      const el = document.createElement('div');
      el.className = 'msg msg-assistant'; el.innerHTML = renderMarkdown(data.summary || '');
      if (data.telemetry) el.appendChild(renderTelemetryPanel(data.telemetry));
      chatBox().appendChild(el); chatBox().scrollTop = chatBox().scrollHeight;
      // Force the done panel — belt AND suspenders
      forceDonePanel();
      await loadConversations(); // update sidebar count without affecting panel
    }
  } catch (e) { hideLoading(); appendMessage(`❌ Error: ${e.message}`, 'assistant', 'text', true); }
}

// ─── Edit modal ────────────────────────────────────────────────────────────────
let _editChangeType = null;
function _setSelect(id, value) {
  const el = document.getElementById(id); if (!el) return;
  for (const opt of el.options) { if (opt.value === value) { opt.selected = true; return; } }
  const opt = document.createElement('option'); opt.value = opt.textContent = value; el.appendChild(opt); opt.selected = true;
}
function _buildAcRows(cid, criteria) {
  document.getElementById(cid).innerHTML = ''; (criteria||[]).forEach(ac => _appendAcRow(cid, ac));
}
function _appendAcRow(cid, val='') {
  const row = document.createElement('div'); row.className = 'edit-ac-row';
  row.innerHTML = `<input type="text" class="edit-input edit-ac-input" value="${esc(val)}" placeholder="Given / When / Then…"/>
    <button class="edit-ac-remove" onclick="this.parentElement.remove()">×</button>`;
  document.getElementById(cid).appendChild(row);
}
function addAcRow()    { _appendAcRow('edit-ac-list'); }
function addNewAcRow() { _appendAcRow('edit-new-ac-list'); }

function showEditModal() {
  document.getElementById('edit-error').textContent = '';
  if (_currentReviewData) _openEditModal(_currentReviewData); else _fetchAndOpenEditModal();
}
async function _fetchAndOpenEditModal() {
  try {
    const r2 = await apiFetch(`/sessions/${currentConvId}/proposed`);
    if (r2.ok) {
      const proposed = await r2.json();
      const sr = await apiFetch(`/sessions/${currentConvId}/status`);
      const st = await sr.json();
      const ch = (proposed.changes||proposed)[st.review_index||0];
      if (ch) { _currentReviewData = ch; _openEditModal(ch); return; }
    }
  } catch {}
  document.getElementById('edit-modal').classList.remove('hidden');
  document.getElementById('edit-error').textContent = 'Could not load change data.';
}
function _openEditModal(change) {
  _editChangeType = change.change_type;
  document.getElementById('edit-form-update').classList.add('hidden');
  document.getElementById('edit-form-create').classList.add('hidden');
  document.getElementById('edit-error').textContent = '';
  if (change.change_type === 'UPDATE' && change.story_update) {
    const u = change.story_update;
    document.getElementById('edit-title').value     = u.updated_title   || '';
    document.getElementById('edit-story').value     = u.updated_story   || '';
    document.getElementById('edit-changelog').value = u.changelog_entry || '';
    _setSelect('edit-priority', u.updated_priority || 'Medium');
    _setSelect('edit-category', u.updated_category || 'Feature');
    _buildAcRows('edit-ac-list', u.updated_acceptance_criteria);
    document.getElementById('edit-form-update').classList.remove('hidden');
  } else if (change.change_type === 'CREATE' && change.new_story) {
    const n = change.new_story;
    document.getElementById('edit-new-title').value = n.title || '';
    document.getElementById('edit-new-story').value = n.story || '';
    _setSelect('edit-new-priority', n.priority || 'Medium');
    _setSelect('edit-new-category', n.category || 'Feature');
    _buildAcRows('edit-new-ac-list', n.acceptance_criteria);
    document.getElementById('edit-form-create').classList.remove('hidden');
  } else {
    document.getElementById('edit-error').textContent = 'No editable fields.';
  }
  document.getElementById('edit-modal').classList.remove('hidden');
}
function closeEditModal() { document.getElementById('edit-modal').classList.add('hidden'); }
function _collectAcRows(cid) {
  return Array.from(document.querySelectorAll(`#${cid} .edit-ac-input`)).map(el => el.value.trim()).filter(Boolean);
}
async function submitEdit() {
  let edited = {};
  if (_editChangeType === 'UPDATE') {
    edited = {
      updated_title:               document.getElementById('edit-title').value.trim(),
      updated_story:               document.getElementById('edit-story').value.trim(),
      updated_priority:            document.getElementById('edit-priority').value,
      updated_category:            document.getElementById('edit-category').value,
      updated_acceptance_criteria: _collectAcRows('edit-ac-list'),
      changelog_entry:             document.getElementById('edit-changelog').value.trim(),
    };
  } else if (_editChangeType === 'CREATE') {
    edited = {
      title:               document.getElementById('edit-new-title').value.trim(),
      story:               document.getElementById('edit-new-story').value.trim(),
      priority:            document.getElementById('edit-new-priority').value,
      category:            document.getElementById('edit-new-category').value,
      acceptance_criteria: _collectAcRows('edit-new-ac-list'),
    };
  }
  closeEditModal(); showLoading('Applying edits…');
  try {
    const r    = await apiFetch(`/sessions/${currentConvId}/review`, { method: 'POST', body: JSON.stringify({ decision: 'EDIT', edited_data: edited }) });
    const data = await r.json();
    hideLoading();
    appendMessage('✏️ Edited & applied', 'user', 'review_decision', true);
    if (data.status === 'next') {
      _currentReviewData = data.change_data || null;
      const el = document.createElement('div');
      el.className = 'msg msg-assistant'; el.innerHTML = renderMarkdown(data.review_card);
      chatBox().appendChild(el); chatBox().scrollTop = chatBox().scrollHeight;
      showPanel('review');
    } else if (data.status === 'done') {
      _pipelineDone = true;
      const el = document.createElement('div');
      el.className = 'msg msg-assistant'; el.innerHTML = renderMarkdown(data.summary || '');
      if (data.telemetry) el.appendChild(renderTelemetryPanel(data.telemetry));
      chatBox().appendChild(el); chatBox().scrollTop = chatBox().scrollHeight;
      forceDonePanel();
      await loadConversations();
    }
  } catch (e) { hideLoading(); appendMessage(`❌ Error: ${e.message}`, 'assistant', 'text', true); }
}

// ─── Follow-up chat (only available in done state) ────────────────────────────
async function sendChat() {
  const input = document.getElementById('user-input');
  const text  = input.value.trim(); if (!text) return;
  input.value = '';
  appendMessage(text, 'user', 'text', true);
  showPanel('processing');
  document.getElementById('processing-label').textContent = 'Thinking…';
  const form = new FormData(); form.append('meeting_notes_text', text);
  try {
    const ur = await fetch(`${API}/sessions/${currentConvId}/upload`, {
      method: 'POST', headers: { 'Authorization': `Bearer ${getToken()}` }, body: form,
    });
    if (!ur.ok) {
      appendMessage(`❌ ${(await ur.json().catch(()=>({}))).detail || 'Request failed'}`, 'assistant', 'text', true);
      forceDonePanel(); return;
    }
    const { route } = await ur.json();
    // _pipelineDone is true here; streamGeneralAnswer → onSSE('answer') → restorePanel() → forceDonePanel()
    if (route === 'MEETING_NOTES') await streamPipeline();
    else await streamGeneralAnswer();
  } catch (e) { appendMessage(`❌ ${e.message}`, 'assistant', 'text', true); forceDonePanel(); }
}

// ─── Download backlog ─────────────────────────────────────────────────────────
async function downloadBacklog() {
  const r = await apiFetch(`/sessions/${currentConvId}/backlog`);
  if (!r.ok) { alert('Backlog not ready yet.'); return; }
  const url = URL.createObjectURL(await r.blob());
  const a   = document.createElement('a'); a.href = url; a.download = 'backlog_updated.json'; a.click();
  URL.revokeObjectURL(url);
}

// ─── Telemetry panel ──────────────────────────────────────────────────────────
function renderTelemetryPanel(t) {
  const fmt = (v, suffix='') => v != null ? v.toLocaleString() + suffix : '—';
  const panel  = document.createElement('div'); panel.className = 'telemetry-panel';
  const header = document.createElement('div'); header.className = 'telemetry-header';
  header.innerHTML = `
    <div class="telemetry-header-left">🔭 Pipeline Telemetry
      <span class="telemetry-badge">${t.llm_calls.length} LLM call${t.llm_calls.length!==1?'s':''}</span>
    </div>
    <span class="telemetry-chevron" id="tel-chevron-${t.run_id}">▼</span>`;
  const body = document.createElement('div'); body.className = 'telemetry-body'; body.id = `tel-body-${t.run_id}`;
  body.innerHTML = `<div class="telemetry-stats">
    <div class="t-stat"><div class="t-stat-label">Total latency</div><div class="t-stat-value amber">${t.latency_ms!=null?(t.latency_ms/1000).toFixed(1)+'s':'—'}</div></div>
    <div class="t-stat"><div class="t-stat-label">Total tokens</div><div class="t-stat-value accent">${fmt(t.total_tokens)}</div></div>
    <div class="t-stat"><div class="t-stat-label">Input tokens</div><div class="t-stat-value">${fmt(t.prompt_tokens)}</div></div>
    <div class="t-stat"><div class="t-stat-label">Output tokens</div><div class="t-stat-value">${fmt(t.completion_tokens)}</div></div>
  </div>`;
  if (t.llm_calls.length > 0) {
    const lbl = document.createElement('div'); lbl.className = 't-calls-label'; lbl.textContent = 'LLM Call Breakdown'; body.appendChild(lbl);
    t.llm_calls.forEach(c => {
      const row = document.createElement('div'); row.className = 't-call-row';
      const ok  = c.status === 'success' || c.status === 'completed';
      row.innerHTML = `<div class="t-call-status ${ok?'success':'error'}"></div><div class="t-call-name">${esc(c.name)}</div>
        <div class="t-call-tokens">${c.total_tokens!=null?c.total_tokens.toLocaleString()+' tok':'—'}</div>
        <div class="t-call-ms">${c.latency_ms!=null?c.latency_ms+'ms':'—'}</div>`;
      body.appendChild(row);
    });
  }
  if (t.langsmith_url) {
    const a = document.createElement('a'); a.className='t-ls-link'; a.href=t.langsmith_url; a.target='_blank'; a.rel='noopener';
    a.innerHTML='🔗 View full trace in LangSmith'; body.appendChild(a);
  }
  header.addEventListener('click', () => {
    const open = body.classList.toggle('open');
    document.getElementById(`tel-chevron-${t.run_id}`).classList.toggle('open', open);
  });
  panel.appendChild(header); panel.appendChild(body); return panel;
}

// ─── Chat helpers ─────────────────────────────────────────────────────────────
function chatBox() { return document.getElementById('chat-box'); }
function appendMessage(content, role, type='text', scroll=true) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role==='user' ? 'msg-user' : type==='file' ? 'msg-file' : 'msg-assistant');
  el.innerHTML = renderMarkdown(content);
  chatBox().appendChild(el);
  if (scroll) chatBox().scrollTop = chatBox().scrollHeight;
}

// ─── Markdown renderer ────────────────────────────────────────────────────────
function renderMarkdown(text) {
  if (!text) return '';
  const lines = text.split('\n'); const out = []; let inUl = false;
  for (const line of lines) {
    if (/^### /.test(line)) { if(inUl){out.push('</ul>');inUl=false;} out.push(`<h3>${inlineEsc(line.slice(4))}</h3>`); continue; }
    if (/^## /.test(line))  { if(inUl){out.push('</ul>');inUl=false;} out.push(`<h2>${inlineEsc(line.slice(3))}</h2>`); continue; }
    if (/^# /.test(line))   { if(inUl){out.push('</ul>');inUl=false;} out.push(`<h1>${inlineEsc(line.slice(2))}</h1>`); continue; }
    if (/^> /.test(line))   { if(inUl){out.push('</ul>');inUl=false;} out.push(`<blockquote>${inlineEsc(line.slice(2))}</blockquote>`); continue; }
    if (/^---+$/.test(line.trim())) { if(inUl){out.push('</ul>');inUl=false;} out.push('<hr/>'); continue; }
    if (/^- /.test(line))   { if(!inUl){out.push('<ul>');inUl=true;} out.push(`<li>${inlineEsc(line.slice(2))}</li>`); continue; }
    if (inUl) { out.push('</ul>'); inUl=false; }
    if (line.trim()==='') { out.push('<br/>'); continue; }
    out.push(`<p>${inlineEsc(line)}</p>`);
  }
  if (inUl) out.push('</ul>');
  return out.join('');
}
function inlineEsc(str) {
  return str.split(/(`[^`]+`)/).map((s,i) => {
    if (i%2===1) return `<code>${esc(s.slice(1,-1))}</code>`;
    return esc(s).replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/_(.+?)_/g,'<em>$1</em>');
  }).join('');
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── Loading overlay ──────────────────────────────────────────────────────────
function showLoading(msg='Processing…') {
  document.getElementById('loader-text').textContent = msg;
  document.getElementById('loading-overlay').classList.remove('hidden');
}
function hideLoading() { document.getElementById('loading-overlay').classList.add('hidden'); }

// ─── Sidebar toggle ───────────────────────────────────────────────────────────
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('collapsed'); }

// ─── Util ─────────────────────────────────────────────────────────────────────
function relTime(iso) {
  const d = Math.floor((Date.now()-new Date(iso))/86400000);
  return d===0?'Today':d===1?'Yesterday':d<7?`${d}d ago`:new Date(iso).toLocaleDateString();
}

// ─── Bootstrap ────────────────────────────────────────────────────────────────
if (getToken()) {
  fetch(`${API}/auth/me`, { headers: authHeaders() })
    .then(r => { if (!r.ok) throw new Error(); return r.json(); })
    .then(async u => { setUserDisplay(u.email); hideAuth(); await init(); })
    .catch(async () => {
      try {
        await refreshToken();
        const u = await (await fetch(`${API}/auth/me`, { headers: authHeaders() })).json();
        setUserDisplay(u.email); hideAuth(); await init();
      } catch { clearTokens(); showAuth(); }
    });
} else { showAuth(); }