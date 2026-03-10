// ─── Config ───────────────────────────────────────────────────────────────────
const API = 'http://localhost:8000/api';

// ─── State ────────────────────────────────────────────────────────────────────
let currentConvId   = null;
let isLoginMode     = true;
let notesFile       = null;   // File object or null
let appInited       = false;

// ─── Theme toggle ─────────────────────────────────────────────────────────────
function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  const icon = document.getElementById('theme-icon');
  icon.className = isLight ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved === 'light') {
    document.body.classList.add('light');
    const icon = document.getElementById('theme-icon');
    if (icon) icon.className = 'fa-solid fa-sun';
  }
}


const getToken      = ()     => localStorage.getItem('access_token');
const getRefreshTok = ()     => localStorage.getItem('refresh_token');
const setTokens     = (a, r) => { localStorage.setItem('access_token', a); localStorage.setItem('refresh_token', r); };
const clearTokens   = ()     => { localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); };

function authHeaders() {
  return { 'Content-Type': 'application/json', 'Authorization': `Bearer ${getToken()}` };
}

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
  let r = await fetch(`${API}${path}`, {
    ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) },
  });
  if (r.status === 401) {
    try {
      await refreshToken();
      r = await fetch(`${API}${path}`, {
        ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) },
      });
    } catch {
      clearTokens(); showAuth(); throw new Error('Session expired');
    }
  }
  return r;
}

// ─── Auth ─────────────────────────────────────────────────────────────────────
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
  document.getElementById('auth-title').textContent = isLoginMode ? 'Sign in' : 'Create account';
  document.getElementById('auth-sub').textContent   = isLoginMode
    ? 'Welcome back — let\'s ship something great.'
    : 'Join BacklogAI and streamline your sprints.';
  document.getElementById('auth-submit').textContent = isLoginMode ? 'Sign in' : 'Register';
  document.getElementById('auth-toggle-text').textContent = isLoginMode
    ? 'No account? Register free'
    : 'Already have an account? Sign in';
  document.getElementById('auth-error').textContent = '';
}

async function submitAuth() {
  const email    = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  const errEl    = document.getElementById('auth-error');
  const btn      = document.getElementById('auth-submit');
  errEl.textContent = '';
  btn.disabled = true; btn.textContent = 'Please wait…';

  try {
    let res;
    if (isLoginMode) {
      const body = new URLSearchParams({ username: email, password });
      res = await fetch(`${API}/auth/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body,
      });
    } else {
      res = await fetch(`${API}/auth/register`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
    }
    const data = await res.json();
    if (!res.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(', ') : (data.detail || `HTTP ${res.status}`));
    setTokens(data.access_token, data.refresh_token);
    setUserDisplay(data.user.email);
    hideAuth();
    await init();
  } catch (e) {
    errEl.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = isLoginMode ? 'Sign in' : 'Register';
  }
}

// Allow Enter key on auth inputs
document.getElementById('auth-email').addEventListener('keydown', e => { if (e.key === 'Enter') submitAuth(); });
document.getElementById('auth-password').addEventListener('keydown', e => { if (e.key === 'Enter') submitAuth(); });

function setUserDisplay(email) {
  document.getElementById('user-email-display').textContent = email;
  document.getElementById('user-avatar').textContent = email[0].toUpperCase();
}

async function doLogout() {
  try {
    await apiFetch('/auth/logout', { method: 'POST', body: JSON.stringify({ refresh_token: getRefreshTok() }) });
  } catch { /* ignore */ }
  clearTokens();
  currentConvId = null; appInited = false;
  document.getElementById('chat-box').innerHTML = '';
  document.getElementById('conversations-list').innerHTML = '';
  showAuth();
}

// ─── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  if (appInited) return;
  appInited = true;
  initTheme();
  const convs = await fetchConversations();
  if (convs.length === 0) {
    await createNewConversation();
  } else {
    renderSidebar(convs);
    await switchConversation(convs[0].id);
  }
}

// ─── Conversations ────────────────────────────────────────────────────────────
async function fetchConversations() {
  try {
    const r = await apiFetch('/conversations');
    return (await r.json()).conversations || [];
  } catch { return []; }
}

async function createNewConversation() {
  const r    = await apiFetch('/conversations', { method: 'POST', body: JSON.stringify({ title: 'New Session' }) });
  const conv = await r.json();
  currentConvId = conv.id;
  document.getElementById('chat-box').innerHTML = '';
  document.getElementById('session-title-display').textContent = conv.title;
  document.getElementById('download-btn').classList.add('hidden');
  showPanel('upload');
  await loadConversations();
}

async function loadConversations() {
  const convs = await fetchConversations();
  renderSidebar(convs);
}

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
  currentConvId = id;
  document.getElementById('chat-box').innerHTML = '';
  await loadMessages(id);
  await loadConversations();
}

async function loadMessages(id) {
  const r    = await apiFetch(`/conversations/${id}/messages`);
  const data = await r.json();
  const box  = document.getElementById('chat-box');
  box.innerHTML = '';

  let hasResult = false;
  (data.messages || []).forEach(m => {
    // Skip hidden system messages
    if (m.msg_type === 'meeting_notes' || m.msg_type === 'review_decisions' || m.msg_type === 'general_query' || m.msg_type === 'pinecone_snapshot') return;
    appendMessage(m.content, m.role, m.msg_type, false);
    if (m.msg_type === 'result') hasResult = true;
  });
  box.scrollTop = box.scrollHeight;

  // Restore correct panel based on stage
  const sr = await apiFetch(`/sessions/${id}/status`);
  const status = await sr.json();
  document.getElementById('session-title-display').textContent = id.slice(0, 8) + '…';

  if (status.stage === 'review') {
    showPanel('review');
  } else if (status.stage === 'done') {
    showPanel('done');
    document.getElementById('download-btn').classList.remove('hidden');
  } else {
    showPanel('upload');
    document.getElementById('download-btn').classList.add('hidden');
  }
}

async function deleteConv(id) {
  if (!confirm('Delete this session?')) return;
  await apiFetch(`/conversations/${id}`, { method: 'DELETE' });
  if (id === currentConvId) { currentConvId = null; await createNewConversation(); }
  else { await loadConversations(); }
}

// ─── Panel management ─────────────────────────────────────────────────────────
function showPanel(name) {
  document.getElementById('upload-panel').classList.toggle('hidden',      name !== 'upload');
  document.getElementById('processing-bar').classList.toggle('hidden',    name !== 'processing');
  document.getElementById('review-panel').classList.toggle('hidden',      name !== 'review');
  document.getElementById('chat-input-bar').classList.toggle('hidden',    name !== 'chat' && name !== 'done' && name !== 'review');
}

// ─── File upload handlers ─────────────────────────────────────────────────────
function handleNotesFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  notesFile = file;
  const chip = document.getElementById('attachment-chip');
  chip.classList.remove('hidden');
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
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function composerKeydown(e) {
  // Ctrl+Enter or Cmd+Enter submits; plain Enter adds newline
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    runPipeline();
  }
}

// ─── Run pipeline ─────────────────────────────────────────────────────────────
async function runPipeline() {
  const errEl    = document.getElementById('upload-error');
  const textArea = document.getElementById('notes-textarea').value.trim();
  errEl.textContent = '';
  errEl.classList.remove('visible');

  if (!notesFile && !textArea) {
    errEl.textContent = '⚠ Attach a file or paste your input first.';
    errEl.classList.add('visible');
    return;
  }

  // Capture input before resetting the composer
  const userText = textArea;
  const userFileName = notesFile ? notesFile.name : null;

  const form = new FormData();
  if (notesFile) form.append('meeting_notes_file', notesFile);
  if (textArea)  form.append('meeting_notes_text', textArea);

  // ── Show user message immediately — don't wait for the API ──
  if (userFileName) {
    appendMessage(`📎 **${userFileName}**`, 'user', 'text', true);
  } else if (userText) {
    appendMessage(userText, 'user', 'text', true);
  }

  // Reset composer
  document.getElementById('notes-textarea').value = '';
  autoResizeComposer(document.getElementById('notes-textarea'));
  removeAttachment();
  showPanel('processing');
  document.getElementById('processing-label').textContent = 'Analysing input…';

  try {
    const ur = await fetch(`${API}/sessions/${currentConvId}/upload`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${getToken()}` },
      body: form,
    });

    if (!ur.ok) {
      const d = await ur.json().catch(() => ({}));
      const msg = d.detail || 'Upload failed';
      showPanel('upload');
      appendMessage(msg, 'assistant', 'text', true);
      return;
    }

    const uploadData = await ur.json();
    const route      = uploadData.route;

    if (route === 'MEETING_NOTES') {
      const chars = uploadData.chars ? uploadData.chars.toLocaleString() : '?';
      appendMessage(`📄 **Meeting notes loaded** · ${chars} chars`, 'assistant', 'text', true);
      await streamPipeline();
    } else {
      // GENERAL_QUESTION — user message already shown above
      await streamGeneralAnswer();
    }

  } catch (e) {
    showPanel('upload');
    appendMessage(`❌ ${e.message}`, 'assistant', 'text', true);
  }
}


async function streamPipeline() {
  showPanel('processing');
  document.getElementById('processing-label').textContent = 'Agent is running…';

  const res = await fetch(`${API}/sessions/${currentConvId}/process/stream`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken()}` },
  });

  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    appendMessage(`❌ ${d.detail || 'Pipeline start failed'}`, 'assistant', 'text', true);
    showPanel('upload');
    return;
  }

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer    = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try {
        const ev = JSON.parse(line.slice(6));
        handleSSEEvent(ev);
      } catch { /* malformed */ }
    }
  }
}

async function streamGeneralAnswer() {
  showPanel('processing');
  document.getElementById('processing-label').textContent = '🌐 Searching the web…';

  const res = await fetch(`${API}/sessions/${currentConvId}/general/stream`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken()}` },
  });

  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    appendMessage(`❌ ${d.detail || 'Search failed'}`, 'assistant', 'text', true);
    showPanel('upload');
    return;
  }

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer    = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try { handleSSEEvent(JSON.parse(line.slice(6))); } catch { /* skip */ }
    }
  }
}

// Holds the current proposed change so the edit modal can pre-populate fields
let _currentReviewData = null;

// ─── SSE event handler ────────────────────────────────────────────────────────
// FIX: was a bare block before — must be a named function for streamPipeline/streamGeneralAnswer to call it
function handleSSEEvent(ev) {
  const box = document.getElementById('chat-box');

  if (ev.type === 'step') {
    // Update the processing bar label instead of cluttering the chat
    const label = document.getElementById('processing-label');
    if (label) label.textContent = ev.message;
    // Also add a subtle step message to chat
    const existing = document.getElementById(`step-${ev.step}`);
    if (!existing) {
      const el = document.createElement('div');
      el.className = 'msg msg-system-step';
      el.id = `step-${ev.step}`;
      el.textContent = ev.message;
      box.appendChild(el);
      box.scrollTop = box.scrollHeight;
    }
  }
  else if (ev.type === 'step_done') {
    const existing = document.getElementById(`step-${ev.step}`);
    if (existing) existing.remove();
    appendMessage(ev.message, 'assistant', 'text', true);
  }
  else if (ev.type === 'info') {
    appendMessage(ev.message, 'assistant', 'text', true);
  }
  else if (ev.type === 'warning') {
    appendMessage(ev.message, 'assistant', 'text', true);
  }
  else if (ev.type === 'review_card') {
    // Cache the proposed change so the edit modal can pre-populate fields
    _currentReviewData = ev.change_data || null;
    // Render the card directly into the chat as HTML from Markdown
    const el = document.createElement('div');
    el.className = 'msg msg-assistant';
    el.innerHTML = renderMarkdown(ev.content);
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    showPanel('review');
  }
  else if (ev.type === 'answer') {
    // Final web-search answer — render as a nicely styled assistant message
    appendMessage(ev.message, 'assistant', 'text', true);
    showPanel('upload');   // ready for next question
  }
  else if (ev.type === 'done') {
    appendMessage(ev.message, 'assistant', 'result', true);
    showPanel('done');  // shows chat-input-bar too
  }
  else if (ev.type === 'error') {
    appendMessage(`❌ ${ev.message}`, 'assistant', 'text', true);
    showPanel('upload');
  }
}


// ─── Review ───────────────────────────────────────────────────────────────────
async function sendReview(decision) {
  showLoading('Saving decision…');
  try {
    const r = await apiFetch(`/sessions/${currentConvId}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    });
    hideLoading();
    const data = await r.json();

    const userLabel = { APPROVE: '✅ Approved', REJECT: '❌ Rejected', EDIT: '✏️ Edited' }[decision];
    appendMessage(userLabel, 'user', 'review_decision', true);

    if (data.status === 'next') {
      _currentReviewData = data.change_data || null;
      const box2 = document.getElementById('chat-box');
      const el2  = document.createElement('div');
      el2.className = 'msg msg-assistant';
      el2.innerHTML = renderMarkdown(data.review_card);
      box2.appendChild(el2);
      box2.scrollTop = box2.scrollHeight;
      showPanel('review');
    } else if (data.status === 'done') {
      appendDoneResult(data.summary, data.telemetry);
      showPanel('done');
      document.getElementById('download-btn').classList.remove('hidden');
    }
  } catch (e) {
    hideLoading();
    appendMessage(`❌ Error: ${e.message}`, 'assistant', 'text', true);
  }
}

// ─── Edit modal ────────────────────────────────────────────────────────────────

// Holds the current proposed change being edited so submitEdit can build the payload
let _editChangeType = null;  // 'UPDATE' | 'CREATE'

function _setSelect(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  for (const opt of el.options) {
    if (opt.value === value) { opt.selected = true; return; }
  }
  // Value not in list — add it dynamically
  const opt = document.createElement('option');
  opt.value = opt.textContent = value;
  el.appendChild(opt);
  opt.selected = true;
}

function _buildAcRows(containerId, criteria) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  (criteria || []).forEach(ac => _appendAcRow(containerId, ac));
}

function _appendAcRow(containerId, value = '') {
  const container = document.getElementById(containerId);
  const row = document.createElement('div');
  row.className = 'edit-ac-row';
  row.innerHTML = `
    <input type="text" class="edit-input edit-ac-input" value="${esc(value)}" placeholder="Given / When / Then…"/>
    <button class="edit-ac-remove" onclick="this.parentElement.remove()">×</button>`;
  container.appendChild(row);
}

function addAcRow()    { _appendAcRow('edit-ac-list'); }
function addNewAcRow() { _appendAcRow('edit-new-ac-list'); }

function showEditModal() {
  document.getElementById('edit-error').textContent = '';
  // If we already have the data cached from SSE, open immediately.
  // Otherwise fetch it from the session status endpoint as a fallback.
  if (_currentReviewData) {
    _openEditModal(_currentReviewData);
  } else {
    _fetchAndOpenEditModal();
  }
}

async function _fetchAndOpenEditModal() {
  try {
    const r    = await apiFetch(`/sessions/${currentConvId}/status`);
    const data = await r.json();
    const idx  = data.review_index || 0;

    // Fetch the full proposed changes list via a dedicated endpoint
    const r2   = await apiFetch(`/sessions/${currentConvId}/proposed`);
    if (r2.ok) {
      const proposed = await r2.json();
      const change   = (proposed.changes || proposed)[idx];
      if (change) {
        _currentReviewData = change;
        _openEditModal(change);
        return;
      }
    }
  } catch { /* fall through */ }
  // Last resort — open with error message
  document.getElementById('edit-modal').classList.remove('hidden');
  document.getElementById('edit-error').textContent = 'Could not load change data. Please apply the main.py backend update.';
}

function _openEditModal(change) {
  _editChangeType = change.change_type;

  // Hide both forms, show the right one
  document.getElementById('edit-form-update').classList.add('hidden');
  document.getElementById('edit-form-create').classList.add('hidden');
  document.getElementById('edit-error').textContent = '';

  if (change.change_type === 'UPDATE' && change.story_update) {
    const u = change.story_update;
    document.getElementById('edit-title').value     = u.updated_title     || '';
    document.getElementById('edit-story').value     = u.updated_story     || '';
    document.getElementById('edit-changelog').value = u.changelog_entry   || '';
    _setSelect('edit-priority', u.updated_priority || 'Medium');
    _setSelect('edit-category', u.updated_category || 'Feature');
    _buildAcRows('edit-ac-list', u.updated_acceptance_criteria);
    document.getElementById('edit-form-update').classList.remove('hidden');

  } else if (change.change_type === 'CREATE' && change.new_story) {
    const n = change.new_story;
    document.getElementById('edit-new-title').value = n.title    || '';
    document.getElementById('edit-new-story').value = n.story    || '';
    _setSelect('edit-new-priority', n.priority || 'Medium');
    _setSelect('edit-new-category', n.category || 'Feature');
    _buildAcRows('edit-new-ac-list', n.acceptance_criteria);
    document.getElementById('edit-form-create').classList.remove('hidden');

  } else {
    document.getElementById('edit-error').textContent = 'This change has no editable fields.';
  }

  document.getElementById('edit-modal').classList.remove('hidden');
}

function closeEditModal() {
  document.getElementById('edit-modal').classList.add('hidden');
}

function _collectAcRows(containerId) {
  return Array.from(
    document.querySelectorAll(`#${containerId} .edit-ac-input`)
  ).map(el => el.value.trim()).filter(Boolean);
}

async function submitEdit() {
  const errEl = document.getElementById('edit-error');
  errEl.textContent = '';

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

  closeEditModal();
  showLoading('Applying edits…');

  try {
    const r = await apiFetch(`/sessions/${currentConvId}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision: 'EDIT', edited_data: edited }),
    });
    hideLoading();
    const data = await r.json();
    appendMessage('✏️ Edited & applied', 'user', 'review_decision', true);

    if (data.status === 'next') {
      const box2 = document.getElementById('chat-box');
      const el2  = document.createElement('div');
      el2.className = 'msg msg-assistant';
      el2.innerHTML = renderMarkdown(data.review_card);
      box2.appendChild(el2);
      box2.scrollTop = box2.scrollHeight;
      showPanel('review');
    } else if (data.status === 'done') {
      appendDoneResult(data.summary, data.telemetry);
      showPanel('done');
      document.getElementById('download-btn').classList.remove('hidden');
    }
  } catch (e) {
    hideLoading();
    appendMessage(`❌ Error: ${e.message}`, 'assistant', 'text', true);
  }
}

// ─── Post-pipeline chat input ─────────────────────────────────────────────────
function sendChat() {
  const input = document.getElementById('user-input');
  const text  = input.value.trim();
  if (!text) return;
  input.value = '';
  appendMessage(text, 'user', 'text', true);
  // Friendly acknowledgement — full Q&A could be added here via a /chat endpoint
  setTimeout(() => {
    appendMessage(
      '💡 The backlog has been updated. You can start a **New Session** to process more meeting notes, or **Download Backlog** to export the result.',
      'assistant', 'text', true
    );
  }, 300);
}

// ─── Download backlog ─────────────────────────────────────────────────────────
async function downloadBacklog() {
  const r = await apiFetch(`/sessions/${currentConvId}/backlog`);
  if (!r.ok) { alert('Backlog not ready yet — complete review first.'); return; }
  const blob = await r.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = 'backlog_updated.json'; a.click();
  URL.revokeObjectURL(url);
}


// ─── Done result with optional telemetry panel ────────────────────────────────
function appendDoneResult(summary, telemetry) {
  const box = document.getElementById('chat-box');
  const el  = document.createElement('div');
  el.className = 'msg msg-assistant';
  el.innerHTML = renderMarkdown(summary);

  if (telemetry) {
    el.appendChild(renderTelemetryPanel(telemetry));
  }

  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function renderTelemetryPanel(t) {
  const totalSec = t.latency_ms != null ? (t.latency_ms / 1000).toFixed(1) + 's' : '—';
  const totalTok = t.total_tokens  != null ? t.total_tokens.toLocaleString()  : '—';
  const inTok    = t.prompt_tokens != null ? t.prompt_tokens.toLocaleString() : '—';
  const outTok   = t.completion_tokens != null ? t.completion_tokens.toLocaleString() : '—';

  const panel = document.createElement('div');
  panel.className = 'telemetry-panel';

  const header = document.createElement('div');
  header.className = 'telemetry-header';
  header.innerHTML = `
    <div class="telemetry-header-left">
      🔭 Pipeline Telemetry
      <span class="telemetry-badge">${t.llm_calls.length} LLM call${t.llm_calls.length !== 1 ? 's' : ''}</span>
    </div>
    <span class="telemetry-chevron" id="tel-chevron-${t.run_id}">▼</span>`;

  const body = document.createElement('div');
  body.className = 'telemetry-body';
  body.id = `tel-body-${t.run_id}`;

  // Summary stats row
  const stats = document.createElement('div');
  stats.className = 'telemetry-stats';
  stats.innerHTML = `
    <div class="t-stat">
      <div class="t-stat-label">Total latency</div>
      <div class="t-stat-value amber">${totalSec}</div>
    </div>
    <div class="t-stat">
      <div class="t-stat-label">Total tokens</div>
      <div class="t-stat-value accent">${totalTok}</div>
    </div>
    <div class="t-stat">
      <div class="t-stat-label">Input tokens</div>
      <div class="t-stat-value">${inTok}</div>
    </div>
    <div class="t-stat">
      <div class="t-stat-label">Output tokens</div>
      <div class="t-stat-value">${outTok}</div>
    </div>`;
  body.appendChild(stats);

  // Per-call breakdown
  if (t.llm_calls.length > 0) {
    const callsLabel = document.createElement('div');
    callsLabel.className = 't-calls-label';
    callsLabel.textContent = 'LLM Call Breakdown';
    body.appendChild(callsLabel);

    t.llm_calls.forEach(call => {
      const row = document.createElement('div');
      row.className = 't-call-row';
      const ms    = call.latency_ms != null ? call.latency_ms + 'ms' : '—';
      const tok   = call.total_tokens != null ? call.total_tokens.toLocaleString() + ' tok' : '—';
      const ok    = call.status === 'success' || call.status === 'completed';
      row.innerHTML = `
        <div class="t-call-status ${ok ? 'success' : 'error'}"></div>
        <div class="t-call-name">${esc(call.name)}</div>
        <div class="t-call-tokens">${tok}</div>
        <div class="t-call-ms">${ms}</div>`;
      body.appendChild(row);
    });
  }

  // LangSmith deep link
  if (t.langsmith_url) {
    const link = document.createElement('a');
    link.className = 't-ls-link';
    link.href   = t.langsmith_url;
    link.target = '_blank';
    link.rel    = 'noopener';
    link.innerHTML = '🔗 View full trace in LangSmith';
    body.appendChild(link);
  }

  // Toggle open/close
  header.addEventListener('click', () => {
    const isOpen = body.classList.toggle('open');
    document.getElementById(`tel-chevron-${t.run_id}`).classList.toggle('open', isOpen);
  });

  panel.appendChild(header);
  panel.appendChild(body);
  return panel;
}

// ─── Chat message helpers ─────────────────────────────────────────────────────
function appendMessage(content, role, type = 'text', scroll = true) {
  const box = document.getElementById('chat-box');
  const el  = document.createElement('div');

  let cls = 'msg ';
  if (role === 'user')      cls += 'msg-user';
  else if (type === 'file') cls += 'msg-file';
  else                      cls += 'msg-assistant';

  el.className = cls;
  el.innerHTML = renderMarkdown(content);
  box.appendChild(el);
  if (scroll) box.scrollTop = box.scrollHeight;
}

// ─── Minimal Markdown renderer ────────────────────────────────────────────────
function renderMarkdown(text) {
  if (!text) return '';

  // Process line by line to correctly handle block vs inline elements
  const lines = text.split('\n');
  const out   = [];
  let inUl    = false;

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];

    // Headings
    if (/^### /.test(line)) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push(`<h3>${inlineEsc(line.slice(4))}</h3>`);
      continue;
    }
    if (/^## /.test(line)) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push(`<h2>${inlineEsc(line.slice(3))}</h2>`);
      continue;
    }
    if (/^# /.test(line)) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push(`<h1>${inlineEsc(line.slice(2))}</h1>`);
      continue;
    }
    // Blockquote
    if (/^> /.test(line)) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push(`<blockquote>${inlineEsc(line.slice(2))}</blockquote>`);
      continue;
    }
    // HR
    if (/^---+$/.test(line.trim())) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      out.push('<hr/>');
      continue;
    }
    // Unordered list item
    if (/^- /.test(line)) {
      if (!inUl) { out.push('<ul>'); inUl = true; }
      out.push(`<li>${inlineEsc(line.slice(2))}</li>`);
      continue;
    }
    // Close list if needed
    if (inUl) { out.push('</ul>'); inUl = false; }
    // Empty line → paragraph break
    if (line.trim() === '') {
      out.push('<br/>');
      continue;
    }
    // Normal paragraph line
    out.push(`<p>${inlineEsc(line)}</p>`);
  }

  if (inUl) out.push('</ul>');
  return out.join('');
}

// Apply inline markdown (bold, italic, code) + HTML-escape the plain text parts
function inlineEsc(str) {
  // Split on inline code spans first to avoid escaping their contents
  return str
    .split(/(`[^`]+`)/)
    .map((seg, i) => {
      if (i % 2 === 1) {
        // Inside backticks — escape content and wrap
        return `<code>${esc(seg.slice(1, -1))}</code>`;
      }
      // Outside backticks — escape then apply bold/italic
      let s = esc(seg);
      s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/_(.+?)_/g,       '<em>$1</em>');
      return s;
    })
    .join('');
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ─── Loading helpers ──────────────────────────────────────────────────────────
function showLoading(msg = 'Processing…') {
  document.getElementById('loader-text').textContent = msg;
  document.getElementById('loading-overlay').classList.remove('hidden');
}
function hideLoading() {
  document.getElementById('loading-overlay').classList.add('hidden');
}

// ─── Sidebar toggle ───────────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
}

// ─── Util ─────────────────────────────────────────────────────────────────────
function relTime(iso) {
  const diff = Date.now() - new Date(iso);
  const d    = Math.floor(diff / 86400000);
  if (d === 0) return 'Today';
  if (d === 1) return 'Yesterday';
  if (d < 7)  return `${d}d ago`;
  return new Date(iso).toLocaleDateString();
}

// ─── Bootstrap ────────────────────────────────────────────────────────────────
if (getToken()) {
  fetch(`${API}/auth/me`, { headers: authHeaders() })
    .then(r => { if (!r.ok) throw new Error(); return r.json(); })
    .then(async user => { setUserDisplay(user.email); hideAuth(); await init(); })
    .catch(async () => {
      try {
        await refreshToken();
        const r = await fetch(`${API}/auth/me`, { headers: authHeaders() });
        const u = await r.json();
        setUserDisplay(u.email); hideAuth(); await init();
      } catch { clearTokens(); showAuth(); }
    });
} else {
  showAuth();
}