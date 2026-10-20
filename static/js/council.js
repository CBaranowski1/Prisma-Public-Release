// js/council.js
// ============================================
// Council mode — modular multi-model deliberation.
// A composer toggle that, when active, sends the prompt to /api/council/run and
// renders the seats deliberating (each tinted with its own color) followed by
// the synthesizer's streamed final answer. Mirrors the compareModule pattern:
// chat.js delegates to councilModule.isActive()/handleCouncilSubmit().
// ============================================
import markdownModule from './markdown.js';

let API_BASE = window.location.origin;
let _active = false;
let _running = false;
let _abort = null;

const ui = () => window.uiModule;
const sessions = () => window.sessionModule;

function _md(text) {
  try { return markdownModule.mdToHtml(text || ''); }
  catch (_) { const d = document.createElement('div'); d.textContent = text || ''; return d.innerHTML; }
}

function _esc(s) {
  const u = ui();
  if (u && u.esc) return u.esc(s);
  const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML;
}

function _scroll() { const u = ui(); if (u && u.scrollHistory) u.scrollHistory(); }

// ── toggle / state ──────────────────────────────────────────────────────────
function isActive() { return _active; }

function _syncButton() {
  const btn = document.getElementById('council-toggle-btn');
  if (btn) {
    btn.classList.toggle('active', _active);
    btn.setAttribute('aria-pressed', _active ? 'true' : 'false');
  }
  const ind = document.getElementById('council-indicator-btn');
  if (ind) ind.style.display = _active ? '' : 'none';
}

function toggleMode() {
  _active = !_active;
  // Council and Compare both hijack the send path; Compare is checked first in
  // chat.js, so turning Council on must turn Compare off or Council would never
  // run.
  if (_active && window.compareModule && window.compareModule.isActive()) {
    try { window.compareModule.deactivate(true); } catch (_) {}
  }
  _syncButton();
}

function deactivate() {
  _active = false;
  _syncButton();
}

// ── rendering ────────────────────────────────────────────────────────────────
function _seatCard(seat) {
  const card = document.createElement('div');
  card.className = 'council-seat';
  card.dataset.seat = seat.id;
  card.style.setProperty('--seat-color', seat.color || '#5b9dff');
  card.innerHTML =
    '<div class="council-seat-head">' +
      '<span class="council-seat-dot"></span>' +
      '<span class="council-seat-name">' + _esc(seat.name || seat.id) + '</span>' +
      '<span class="council-seat-role">' + _esc(seat.role || '') + '</span>' +
      '<span class="council-seat-status" data-status>waiting</span>' +
    '</div>' +
    '<div class="council-seat-body"></div>';
  return card;
}

function _setSeatStatus(turn, seatId, status, errMsg) {
  const card = turn.querySelector('.council-seat[data-seat="' + CSS.escape(seatId) + '"]');
  if (!card) return;
  card.classList.remove('running', 'done', 'error');
  if (status) card.classList.add(status);
  const s = card.querySelector('[data-status]');
  if (s) s.textContent = status === 'error' ? ('error' + (errMsg ? ': ' + errMsg : '')) : (status || '');
}

function _setSeatBody(turn, seatId, html) {
  const card = turn.querySelector('.council-seat[data-seat="' + CSS.escape(seatId) + '"]');
  if (!card) return;
  const body = card.querySelector('.council-seat-body');
  if (body) body.innerHTML = html;
}

// ── run ──────────────────────────────────────────────────────────────────────
async function handleCouncilSubmit() {
  if (_running) { if (_abort) _abort.abort(); return; }

  const input = document.getElementById('message');
  const prompt = (input && input.value || '').trim();
  if (!prompt) return;
  if (input) { input.value = ''; input.style.height = 'auto'; }

  // Leave the welcome state, like a normal send.
  const container = document.getElementById('chat-container');
  if (container) container.classList.remove('welcome-active');
  const welcome = document.getElementById('welcome-screen');
  if (welcome) welcome.style.display = 'none';

  const box = document.getElementById('chat-history');
  if (!box) return;

  // User bubble.
  const userMsg = document.createElement('div');
  userMsg.className = 'msg msg-user';
  userMsg.innerHTML = '<div class="body">' + _md(prompt) + '</div>';
  box.appendChild(userMsg);

  // Council turn shell.
  const turn = document.createElement('div');
  turn.className = 'msg msg-ai council-turn';
  turn.innerHTML =
    '<div class="role council-role">⚖️ Council</div>' +
    '<div class="council-grid"></div>' +
    '<div class="council-final"><div class="body council-final-body"></div></div>';
  box.appendChild(turn);
  _scroll();

  const grid = turn.querySelector('.council-grid');
  const finalBody = turn.querySelector('.council-final-body');
  let synthBuf = '';
  let finalAnswer = '';

  _running = true;
  _abort = new AbortController();
  try {
    const reasoningEffort = window.chatModule && window.chatModule.getReasoningEffort
      ? window.chatModule.getReasoningEffort()
      : 'auto';
    const res = await fetch(API_BASE + '/api/council/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ prompt, reasoning_effort: reasoningEffort }),
      signal: _abort.signal,
    });
    if (!res.ok || !res.body) {
      finalBody.innerHTML = '<div class="council-error">Council request failed (' + res.status + ').</div>';
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let streaming = true;
    while (streaming) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop() || '';
      for (const frame of frames) {
        const line = frame.trim();
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data === '[DONE]') { streaming = false; break; }
        let ev;
        try { ev = JSON.parse(data); } catch (_) { continue; }
        switch (ev.type) {
          case 'run_started':
            grid.innerHTML = '';
            (ev.seats || []).forEach(s => grid.appendChild(_seatCard(s)));
            break;
          case 'seat_status':
            _setSeatStatus(turn, ev.seat_id, ev.status, ev.error);
            break;
          case 'seat_result':
            _setSeatBody(turn, ev.seat_id, _md(ev.content));
            break;
          case 'synthesis_start':
            synthBuf = '';
            // Stream into a cheap pre-wrap text node; markdown is rendered once
            // at the end (re-parsing the whole buffer per token is O(n²)).
            finalBody.innerHTML = '<div class="council-stream" style="white-space:pre-wrap"></div>';
            turn.querySelector('.council-final').classList.add('active');
            break;
          case 'synthesis_delta': {
            synthBuf += ev.delta || '';
            const streamEl = finalBody.querySelector('.council-stream');
            if (streamEl) streamEl.textContent = synthBuf;
            _scroll();
            break;
          }
          case 'done':
            finalAnswer = ev.final || synthBuf;
            if (finalAnswer) finalBody.innerHTML = _md(finalAnswer);
            break;
          case 'error':
            finalBody.innerHTML = '<div class="council-error">' + _esc(ev.error || 'Council error') + '</div>';
            break;
        }
      }
    }

    turn.classList.add('council-done');
    if (!finalAnswer) finalAnswer = synthBuf;
    await _persist(prompt, finalAnswer);
  } catch (e) {
    if (!(e && e.name === 'AbortError')) {
      finalBody.innerHTML = '<div class="council-error">' + _esc(String(e)) + '</div>';
    }
  } finally {
    _running = false;
    _abort = null;
    _scroll();
  }
}

// Persist the turn so it survives a refresh (best-effort; live DOM already shows it).
async function _persist(prompt, answer) {
  try {
    const sid = sessions() && sessions().getCurrentSessionId && sessions().getCurrentSessionId();
    if (!sid || !answer) return;
    await fetch(API_BASE + '/api/session/' + encodeURIComponent(sid) + '/inject_messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ messages: [
        { role: 'user', content: prompt },
        { role: 'assistant', content: answer, metadata: JSON.stringify({ council: true }) },
      ] }),
    });
  } catch (_) { /* non-fatal */ }
}

// ── init ──────────────────────────────────────────────────────────────────────
function init(apiBase) {
  if (apiBase) API_BASE = apiBase;
  const btn = document.getElementById('council-toggle-btn');
  if (btn && !btn._wired) {
    btn._wired = true;
    btn.addEventListener('click', () => toggleMode());
  }
  const ind = document.getElementById('council-indicator-btn');
  if (ind && !ind._wired) {
    ind._wired = true;
    // X on the council indicator must fully exit council AND revert the composer
    // to chat mode. window.setMode('chat') deactivates council (app.js setMode)
    // and switches the mode pill + chat/agent buttons back; fall back to a bare
    // deactivate() only if the mode switcher isn't wired yet.
    ind.addEventListener('click', () => {
      if (typeof window.setMode === 'function') window.setMode('chat');
      else deactivate();
    });
  }
  _syncButton();
}

const councilModule = { init, isActive, toggleMode, deactivate, handleCouncilSubmit };
export default councilModule;
window.councilModule = councilModule;
