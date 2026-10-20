// Prisma UI — Initialization Scripts
// ES6 module — extracted from index.html inline scripts

import Storage from './storage.js';

// Prisma Desktop is permanently single-user. Give the local owner the full
// interface without asking an authentication endpoint to establish a role.
window._isAdmin = true;
window._role = 'admin';
window.__prismaRealAdmin = true;
window.__prismaEffectiveRole = 'admin';

function clearFreshComposerRestore() {
  const msgInput = document.getElementById('message');
  if (!msgInput) return;
  const hasSessionTarget = !!(window.location.hash || Storage.get('lastSessionId'));
  if (hasSessionTarget) return;
  if (msgInput.value) {
    msgInput.value = '';
    msgInput.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

clearFreshComposerRestore();
window.addEventListener('pageshow', clearFreshComposerRestore);

// SECURITY: defense-in-depth state wipe on user switch. If the authenticated
// user is different from the one whose state is cached in this browser,
// wipe localStorage + sessionStorage so the new account doesn't inherit
// the previous user's last session id, last-used model, draft chat input,
// or cached lists. The settings-tab Logout button already wipes on
// explicit logout; this catches the cases where a different user signs
// in without the previous one logging out cleanly.
(async () => {
  try {
    const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json().catch(() => ({}));
    const liveUser = (data && data.username) || '';
    if (!liveUser) return;
    const KEY = 'prisma-auth-user';
    const cachedUser = localStorage.getItem(KEY);
    if (cachedUser && cachedUser !== liveUser) {
      const _keepKeys = new Set(['prisma-last-user', KEY]);
      const toRemove = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && !_keepKeys.has(k)) toRemove.push(k);
      }
      toRemove.forEach(k => localStorage.removeItem(k));
      sessionStorage.clear();
      clearFreshComposerRestore();
    }
    localStorage.setItem(KEY, liveUser);
    // Apply per-user privilege gates to the UI. The backend enforces these
    // independently — this is purely cosmetic / "don't dangle controls the
    // user can't actually use." Privileges come from /api/auth/status; admins
    // always get the full set so this is a no-op for them.
    try {
      const privs = (data && data.privileges) || {};
      const hideOn = (selector, allowed) => {
        if (allowed === undefined || allowed === true) return;
        document.querySelectorAll(selector).forEach(el => {
          el.style.display = 'none';
        });
      };
      // Document editor — overflow menu button + the docs panel rail/tool button.
      hideOn('#overflow-doc-btn, #tool-doc-btn', privs.can_use_documents);
      // Research — the in-input deep-research toggle (sidebar block owned by layout registry).
      hideOn('#research-toggle-btn', privs.can_use_research);
      // Effective role: a dev "View as" override (admin only) can shadow the
      // real role for UI preview. Server still enforces real permissions.
      const realRole = (data && data.role) || 'user';
      const realAdmin = !!(data && data.is_admin);
      let effRole = realRole;
      try {
        const ov = sessionStorage.getItem('prisma-view-as');
        if (ov && realAdmin) effRole = ov;   // only real admins may preview
      } catch (_) {}
      window._role = realRole;
      window.__prismaRealAdmin = realAdmin;        // never affected by preview
      window.__prismaEffectiveRole = effRole;
      const canBrain  = effRole === 'admin' || effRole === 'coder';
      const canModels = effRole === 'admin' || effRole === 'coder';
      // Document-wide role-class visibility. NOTE: do NOT clobber window._isAdmin
      // (models.js reads it) — use dedicated classes instead.
      //   .coder-only     → visible to coder + admin (effective role)
      //   .realadmin-only → visible only to a REAL admin (the switcher itself)
      document.querySelectorAll('.coder-only').forEach(el => { el.style.display = canBrain ? '' : 'none'; });
      document.querySelectorAll('.realadmin-only').forEach(el => { el.style.display = realAdmin ? '' : 'none'; });
      // Apply the per-user customizable sidebar layout (role default overlaid
      // by the user's saved pref; preview ignores the pref). Replaces the old
      // per-role class toggle. Dynamic import keeps init.js dependency-light.
      const _isPreview = !!(realAdmin && (() => { try { return !!sessionStorage.getItem('prisma-view-as'); } catch (_) { return false; } })());
      import('./sidebarLayout.js').then(m => m.initSidebarLayout({ role: effRole, privileges: privs, isPreview: _isPreview }))
        .catch(e => console.warn('sidebar layout init failed:', e));
      // In-chat "Add model endpoints" button — coder/admin only.
      if (!canModels) hideOn('#model-picker-add-models-btn', false);
      // Agent mode toggle — force chat mode by hiding the Agent toggle button.
      if (privs.can_use_agent === false) {
        const _agent = document.getElementById('mode-agent-btn');
        const _chat = document.getElementById('mode-chat-btn');
        if (_agent) _agent.style.display = 'none';
        if (_chat) { _chat.classList.add('active'); _chat.click?.(); }
      }
    } catch (_) { /* DOM not ready or unexpected shape — UI gates are non-fatal */ }
  } catch (_) { /* anonymous / loopback mode — nothing to do */ }
})();

/* Sidebar section default-collapsed setup. The click-to-toggle handlers
   themselves live in js/section-management.js — attaching them in BOTH
   places caused two toggles per click, which read as "clicks aren't doing
   anything" (even-count parity). Keep only the initial-state-apply here. */
{
  const KEY = Storage.KEYS.SIDEBAR_COLLAPSED;
  const saved = Storage.getJSON(KEY, {});
  const _defaultCollapsed = { 'sessions-section': true };
  document.querySelectorAll('.sidebar .section').forEach((section) => {
    const id = section.id;
    if (!id) return;
    const shouldCollapse = (id in saved) ? saved[id] : !!_defaultCollapsed[id];
    if (shouldCollapse) section.classList.add('collapsed');
  });
  // Sessions-section notification dot: clear when the section becomes
  // expanded. Watch the class with MutationObserver so we don't need a
  // click handler (which would race the section-management one).
  const sessionsSection = document.getElementById('sessions-section');
  if (sessionsSection) {
    new MutationObserver(() => {
      if (!sessionsSection.classList.contains('collapsed')) {
        const dot = document.getElementById('chats-notif-dot');
        if (dot) dot.style.display = 'none';
      }
    }).observe(sessionsSection, { attributes: true, attributeFilter: ['class'] });
  }
}

/* Publish sidebar width as --sidebar-w CSS var so fullscreen panels can
   reserve space on the left. --icon-rail-w is pinned to 0px (the legacy
   icon-rail has been removed); it remains defined so any consumer that
   reads it still gets a valid value. Updates live as the sidebar resizes
   or its hidden class toggles. */
{
  const sidebar = document.getElementById('sidebar');
  const root = document.documentElement;
  const _measure = (el) => {
    if (!el) return null;
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return 0;
    return Math.round(el.getBoundingClientRect().width);
  };
  const _sync = () => {
    // Left gutter is the sidebar only now (the icon-rail was removed); the
    // legacy --icon-rail-w stays defined (0) for any consumer that reads it.
    root.style.setProperty('--icon-rail-w', '0px');
    const sw = _measure(sidebar);
    if (sw === null) {
      root.style.removeProperty('--sidebar-w');
    } else {
      root.style.setProperty('--sidebar-w', sw + 'px');
    }
  };
  _sync();
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(_sync);
    if (sidebar) ro.observe(sidebar);
  }
  if (sidebar && typeof MutationObserver !== 'undefined') {
    new MutationObserver(_sync).observe(sidebar, { attributes: true, attributeFilter: ['class', 'style'] });
  }
  window.addEventListener('resize', _sync);
}

/* Keep minimized tool chips above the composer. Both the current modalManager
   dock and the legacy fallback dock consume this root-level clearance. */
{
  const root = document.documentElement;
  const chatBar = document.querySelector('.chat-input-bar');
  const attachStrip = document.getElementById('attach-strip');
  const chatContainer = document.getElementById('chat-container');
  const _syncComposerClearance = () => {
    let top = window.innerHeight;
    for (const el of [attachStrip, chatBar]) {
      if (!el) continue;
      const rect = el.getBoundingClientRect();
      if (rect.height > 0) top = Math.min(top, rect.top);
    }
    const clearance = Math.max(12, Math.ceil(window.innerHeight - top + 8));
    root.style.setProperty('--composer-clearance', clearance + 'px');
  };
  requestAnimationFrame(_syncComposerClearance);
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(_syncComposerClearance);
    if (chatBar) ro.observe(chatBar);
    if (attachStrip) ro.observe(attachStrip);
  }
  if (chatContainer && typeof MutationObserver !== 'undefined') {
    new MutationObserver(_syncComposerClearance).observe(chatContainer, {
      attributes: true,
      attributeFilter: ['class'],
    });
  }
  if (chatBar) chatBar.addEventListener('transitionend', _syncComposerClearance);
  window.addEventListener('resize', _syncComposerClearance);
}

/* ---- Resizable sidebar — drag edge to resize, collapse if small, drag rail edge to expand ---- */
{
  const sidebar = document.getElementById('sidebar');
  const handle = document.getElementById('sidebar-resize-handle');
  const railHandle = document.getElementById('rail-resize-handle');
  if (sidebar && handle) {

  const STORAGE_KEY = Storage.KEYS.SIDEBAR_WIDTH;
  const MIN_WIDTH = 200;
  const MAX_WIDTH = 700;
  const COLLAPSE_THRESHOLD = 150;

  function getSavedWidth() {
    const w = parseInt(Storage.get(STORAGE_KEY, '340'), 10);
    return (w >= MIN_WIDTH && w <= MAX_WIDTH) ? w : 340;
  }

  // Restore saved width
  const savedWidth = Storage.get(STORAGE_KEY);
  if (savedWidth) {
    const w = parseInt(savedWidth, 10);
    if (w >= MIN_WIDTH && w <= MAX_WIDTH) sidebar.style.width = w + 'px';
  }

  let startX, startWidth, isRight, collapsed, expanding;

  // --- Drag from sidebar edge to resize / collapse ---
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    expanding = false;
    isRight = sidebar.classList.contains('right-side');
    startX = e.clientX;
    startWidth = sidebar.getBoundingClientRect().width;
    collapsed = false;
    sidebar.classList.add('resizing');
    handle.classList.add('dragging');
    document.addEventListener('mousemove', onDrag);
    document.addEventListener('mouseup', stopDrag);
  });

  // --- Drag from icon rail edge to expand sidebar ---
  if (railHandle) {
    railHandle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      expanding = true;
      isRight = sidebar.classList.contains('right-side');
      startX = e.clientX;
      collapsed = false;

      // Unhide sidebar at 0 width so we can grow it
      sidebar.classList.remove('hidden');
      sidebar.classList.add('resizing');
      sidebar.style.width = '0px';
      sidebar.style.opacity = '0.3';
      railHandle.classList.add('dragging');

      document.addEventListener('mousemove', onExpandDrag);
      document.addEventListener('mouseup', stopExpandDrag);
    });
  }

  function onDrag(e) {
    const delta = isRight ? (startX - e.clientX) : (e.clientX - startX);
    const rawWidth = startWidth + delta;

    if (rawWidth < COLLAPSE_THRESHOLD) {
      sidebar.style.width = Math.max(0, rawWidth) + 'px';
      sidebar.style.opacity = Math.max(0.2, rawWidth / COLLAPSE_THRESHOLD);
      collapsed = true;
    } else {
      const newWidth = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, rawWidth));
      sidebar.style.width = newWidth + 'px';
      sidebar.style.opacity = '';
      collapsed = false;
    }
  }

  function stopDrag() {
    sidebar.classList.remove('resizing');
    handle.classList.remove('dragging');
    sidebar.style.opacity = '';
    document.removeEventListener('mousemove', onDrag);
    document.removeEventListener('mouseup', stopDrag);

    if (collapsed) {
      sidebar.style.width = '';
      sidebar.classList.add('hidden');
      if (typeof syncRailSide === 'function') syncRailSide();
    } else {
      const finalWidth = parseInt(sidebar.style.width, 10);
      if (finalWidth >= MIN_WIDTH) {
        Storage.set(STORAGE_KEY, String(finalWidth));
      }
    }
  }

  function onExpandDrag(e) {
    const delta = isRight ? (startX - e.clientX) : (e.clientX - startX);
    const rawWidth = Math.max(0, delta);

    if (rawWidth < COLLAPSE_THRESHOLD) {
      sidebar.style.width = rawWidth + 'px';
      sidebar.style.opacity = Math.max(0.3, rawWidth / COLLAPSE_THRESHOLD);
      collapsed = true;
    } else {
      const newWidth = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, rawWidth));
      sidebar.style.width = newWidth + 'px';
      sidebar.style.opacity = '';
      collapsed = false;
    }
  }

  function stopExpandDrag() {
    sidebar.classList.remove('resizing');
    sidebar.style.opacity = '';
    if (railHandle) railHandle.classList.remove('dragging');
    document.removeEventListener('mousemove', onExpandDrag);
    document.removeEventListener('mouseup', stopExpandDrag);

    if (collapsed) {
      // Didn't drag far enough — snap back to icon rail
      sidebar.style.width = '';
      sidebar.classList.add('hidden');
      if (typeof syncRailSide === 'function') syncRailSide();
    } else {
      // Expanded — save width and sync
      const finalWidth = parseInt(sidebar.style.width, 10);
      if (finalWidth >= MIN_WIDTH) {
        Storage.set(STORAGE_KEY, String(finalWidth));
      }
      if (typeof syncRailSide === 'function') syncRailSide();
    }
  }

  } // end if (sidebar && handle)
}

/* ---- Mobile viewport fix — keep chat visible when virtual keyboard opens ---- */
{
  if (window.visualViewport) {
    let _lastVVHeight = window.visualViewport.height;
    window.visualViewport.addEventListener('resize', function() {
      const vv = window.visualViewport;
      const keyboardOpened = vv.height < _lastVVHeight - 50;
      _lastVVHeight = vv.height;
      if (keyboardOpened) {
        var chatHistory = document.getElementById('chat-history');
        if (chatHistory) {
          requestAnimationFrame(function() {
            chatHistory.scrollTop = chatHistory.scrollHeight;
          });
        }
      }
    });
  }

  // Fade welcome screen when mobile keyboard opens (input focus/blur)
  if ('ontouchstart' in window) {
    document.addEventListener('DOMContentLoaded', function() {
      var _msgInput = document.getElementById('message');
      if (!_msgInput) return;
      _msgInput.addEventListener('focus', function() {
        var welcome = document.getElementById('welcome-screen');
        if (welcome && !welcome.classList.contains('hidden')) {
          welcome.style.transition = 'opacity 0.2s ease, transform 0.2s ease';
          welcome.style.opacity = '0';
          welcome.style.transform = 'translate(-50%, -50%) scale(0.92)';
        }
      });
      _msgInput.addEventListener('blur', function() {
        var welcome = document.getElementById('welcome-screen');
        if (welcome && !welcome.classList.contains('hidden')) {
          welcome.style.transition = 'opacity 0.3s ease, transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)';
          welcome.style.opacity = '';
          welcome.style.transform = '';
        }
      });
    });
  }
}

/* ── Release welcome-screen entrance animations once the page is settled ──
   The splash's entrance animations (#welcome-screen / .welcome-name) are held
   by CSS (`body:not(.welcome-ready)`) until this runs, so they no longer play
   while fonts are loading and the layout is still shifting on first paint
   (which made the splash "go haywire"). We flip the flag after fonts are ready
   plus a couple of frames, with load + timeout fallbacks so the splash is never
   left hidden. Lives here (a network-first module) rather than inline in
   index.html so it updates in lockstep with the gating CSS. */
(function () {
  let fired = false;
  function release() {
    if (fired) return;
    fired = true;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => document.body.classList.add('welcome-ready'))
    );
  }
  try { if (document.fonts && document.fonts.ready) document.fonts.ready.then(release); } catch (_) {}
  if (document.readyState === 'complete') release();
  else window.addEventListener('load', release);
  setTimeout(release, 1200);  // hard fallback — never leave the splash hidden
})();
