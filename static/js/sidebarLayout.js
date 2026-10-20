// Sidebar layout: per-user show/hide + reorder over per-role defaults.
// Pure compute logic (computeLayout) is DOM-free and unit-tested via Node.
// DOM application + editor live below and only touch document inside functions.

const ROLE_RANK = { user: 0, coder: 1, admin: 2 };

// Each block: id (stable logical id), label (editor), el (sidebar DOM id),
// defaultOrder, defaultHiddenByRole,
// requiredRole (coder/admin gate) / requiredPrivilege (privilege gate).
export const BLOCKS = [
  { id: 'chat',     label: 'Chat',          el: 'sidebar-chat-btn',     defaultOrder: 5 },
  { id: 'new-chat', label: 'New Chat',      el: 'sidebar-new-chat-btn', defaultOrder: 10 },
  { id: 'search',   label: 'Search',        el: 'sidebar-search-btn',   defaultOrder: 20 },
  { id: 'sessions', label: 'Chats',         el: 'sessions-section',     defaultOrder: 30 },
  { id: 'models',   label: 'Models',        el: 'models-section',       defaultOrder: 40 },
  { id: 'brain',    label: 'Brain',         el: 'tool-memory-btn',      defaultOrder: 50, requiredRole: 'coder' },
  { id: 'ide',      label: 'Model IDE',     el: 'sidebar-ide-btn',      defaultOrder: 45, requiredRole: 'coder' },
  { id: 'history',  label: 'History',       el: 'sidebar-history-btn',  defaultOrder: 55 },
  { id: 'compare',  label: 'Compare',       el: 'tool-compare-btn',     defaultOrder: 60,  defaultHiddenByRole: { user: true } },
  { id: 'research', label: 'Deep Research', el: 'tool-research-btn',    defaultOrder: 70,  defaultHiddenByRole: { user: true }, requiredPrivilege: 'can_use_research' },
  { id: 'gallery',  label: 'Gallery',       el: 'tool-gallery-btn',     defaultOrder: 80,  defaultHiddenByRole: { user: true } },
  { id: 'library',  label: 'Library',       el: 'tool-library-btn',     defaultOrder: 90,  defaultHiddenByRole: { user: true } },
  { id: 'tasks',    label: 'Tasks',         el: 'tool-tasks-btn',       defaultOrder: 100 },
  { id: 'theme',    label: 'Theme',         el: 'tool-theme-btn',       defaultOrder: 110, defaultHiddenByRole: { user: true } },
];

function meetsRole(required, role) {
  if (!required) return true;
  return (ROLE_RANK[role] ?? 0) >= (ROLE_RANK[required] ?? 0);
}
function meetsPriv(required, privileges) {
  if (!required) return true;
  return !!(privileges && privileges[required]);
}

// Pure: returns { allowedIds, orderedIds, hiddenIds } for the given inputs.
// savedPref is {order?:string[], hidden?:string[]} | null | {} (empty = no override).
export function computeLayout({ role, privileges, savedPref, isPreview }) {
  const allowed = BLOCKS.filter(b => meetsRole(b.requiredRole, role) && meetsPriv(b.requiredPrivilege, privileges));
  const allowedIds = allowed.map(b => b.id);
  const baseOrder = allowed.slice().sort((a, b) => a.defaultOrder - b.defaultOrder).map(b => b.id);
  const baseHidden = allowed.filter(b => b.defaultHiddenByRole && b.defaultHiddenByRole[role]).map(b => b.id);

  const hasPref = !isPreview && savedPref && Array.isArray(savedPref.order) && savedPref.order.length > 0;

  if (!hasPref) {
    return { allowedIds, orderedIds: baseOrder, hiddenIds: baseHidden };
  }
  // Order: start from the saved order (filtered to allowed, deduped). Any
  // allowed id NOT in the saved pref is a newly-shipped block: insert it at the
  // position implied by its defaultOrder (right after its nearest lower-order
  // neighbour already placed) instead of appending at the bottom — so e.g. a
  // new "Chat" block lands at the top by default, not last. A malformed pref
  // still can't duplicate a block (dedupe via _seen).
  const _seen = new Set();
  const orderedIds = savedPref.order.filter(id => allowedIds.includes(id) && !_seen.has(id) && _seen.add(id));
  const missing = baseOrder.filter(id => !_seen.has(id)); // defaultOrder ascending
  for (const id of missing) {
    const bi = baseOrder.indexOf(id);
    let insertAt = 0; // no lower-order neighbour placed yet → front
    for (let i = bi - 1; i >= 0; i--) {
      const pos = orderedIds.indexOf(baseOrder[i]);
      if (pos !== -1) { insertAt = pos + 1; break; }
    }
    orderedIds.splice(insertAt, 0, id);
  }
  // Hidden: user pref fully determines hidden (filtered to allowed).
  const hiddenIds = (Array.isArray(savedPref.hidden) ? savedPref.hidden : []).filter(id => allowedIds.includes(id));
  return { allowedIds, orderedIds, hiddenIds };
}

// ── DOM application ───────────────────────────────────────────────
const PREF_KEY = 'sidebar-layout';

function blockById(id) { return BLOCKS.find(b => b.id === id) || null; }

// Apply a computed layout to the DOM: order sidebar blocks via CSS `order`,
// hide hidden/forbidden blocks.
export function applyLayout({ allowedIds, orderedIds, hiddenIds }) {
  const hidden = new Set(hiddenIds);
  const order = new Map(orderedIds.map((id, i) => [id, i]));
  // Only ever CLEAR a display:none that WE set (marked via data-layout-hidden).
  // Other systems also hide these elements — admin feature gates (app.js) and
  // privilege gates (init.js). Blindly writing display='' would re-show a
  // globally-disabled tool. So we never touch an element another system hid.
  const setVisibility = (el, isHidden) => {
    if (!el) return;
    if (isHidden) {
      el.style.display = 'none';
      el.dataset.layoutHidden = '1';
    } else if (el.dataset.layoutHidden) {
      el.style.display = '';
      delete el.dataset.layoutHidden;
    }
  };
  for (const b of BLOCKS) {
    const sideEl = document.getElementById(b.el);
    const forbidden = !allowedIds.includes(b.id);
    const isHidden = forbidden || hidden.has(b.id);
    setVisibility(sideEl, isHidden);
    if (sideEl && order.has(b.id)) sideEl.style.order = String(order.get(b.id));
  }
}

export async function loadSavedPref() {
  try {
    const res = await fetch(`/api/prefs/${PREF_KEY}`, { credentials: 'same-origin' });
    if (!res.ok) return null;
    const data = await res.json();
    return data && data.value ? data.value : null;
  } catch (_) { return null; }
}

export async function savePref(value) {
  try {
    await fetch(`/api/prefs/${PREF_KEY}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ value }),
    });
  } catch (e) { console.warn('sidebar-layout save failed:', e); }
}

// Called once at load. Reads effective role/privileges + saved pref, computes,
// applies. Keyed on the EFFECTIVE role; in a "View as" preview the personal
// pref is ignored so the preview shows the role default.
export async function initSidebarLayout({ role, privileges, isPreview }) {
  // First pass: apply the role default synchronously (no pref fetch) so
  // default-hidden blocks don't flash visible while the saved pref loads.
  applyLayout(computeLayout({ role, privileges, savedPref: null, isPreview }));
  if (isPreview) return;            // preview ignores the personal pref
  const savedPref = await loadSavedPref();
  if (savedPref) applyLayout(computeLayout({ role, privileges, savedPref, isPreview }));
}

// ── Settings → Sidebar editor ─────────────────────────────────────
let _editorState = null; // { order: string[], hidden: Set<string> }
let _editorRole = 'user';
let _editorPrivs = {};
let _saveTimer = null;
let _dragId = null;      // block id currently being dragged in the editor

function _scheduleSave() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    savePref({ order: _editorState.order, hidden: [..._editorState.hidden] });
  }, 400);
}

function _reapplyFromEditor() {
  const pref = { order: _editorState.order, hidden: [..._editorState.hidden] };
  applyLayout(computeLayout({ role: _editorRole, privileges: _editorPrivs, savedPref: pref, isPreview: false }));
}

function _renderEditor() {
  const list = document.getElementById('sidebar-editor-list');
  if (!list) return;
  const labelOf = id => (blockById(id)?.label || id);
  list.innerHTML = '';
  _editorState.order.forEach((id) => {
    const row = document.createElement('div');
    row.className = 'sb-edit-row';
    row.draggable = true;
    row.dataset.blockId = id;
    row.dataset.hidden = _editorState.hidden.has(id) ? '1' : '0';
    row.innerHTML =
      `<span class="sb-edit-grip" aria-hidden="true" title="Drag to reorder">⠿</span>` +
      `<span class="sb-edit-label">${labelOf(id)}</span>` +
      `<label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;">` +
      `<input type="checkbox" data-act="show" ${_editorState.hidden.has(id) ? '' : 'checked'}> Show</label>`;
    row.querySelector('[data-act="show"]').addEventListener('change', (e) => {
      if (e.target.checked) _editorState.hidden.delete(id); else _editorState.hidden.add(id);
      _onEditorChange();
    });
    // Drag-to-reorder.
    row.addEventListener('dragstart', (e) => {
      _dragId = id;
      row.classList.add('dragging');
      try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', id); } catch (_) {}
    });
    row.addEventListener('dragend', () => {
      _dragId = null;
      list.querySelectorAll('.sb-edit-row').forEach(r => r.classList.remove('dragging', 'drop-before', 'drop-after'));
    });
    row.addEventListener('dragover', (e) => {
      if (!_dragId || _dragId === id) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = 'move'; } catch (_) {}
      const rect = row.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      row.classList.toggle('drop-before', before);
      row.classList.toggle('drop-after', !before);
    });
    row.addEventListener('dragleave', () => { row.classList.remove('drop-before', 'drop-after'); });
    row.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!_dragId || _dragId === id) return;
      const rect = row.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      _reorder(_dragId, id, before);
    });
    list.appendChild(row);
  });
}

// Move dragId to just before/after targetId in the order, then re-apply/save.
function _reorder(dragId, targetId, insertBefore) {
  const arr = _editorState.order;
  const from = arr.indexOf(dragId);
  if (from < 0) return;
  arr.splice(from, 1);
  let to = arr.indexOf(targetId);
  if (to < 0) { arr.splice(from, 0, dragId); return; }  // target gone — restore
  if (!insertBefore) to += 1;
  arr.splice(to, 0, dragId);
  _onEditorChange();
}

function _onEditorChange() {
  _renderEditor();
  _reapplyFromEditor();
  _scheduleSave();
}

// True when a real admin is currently previewing another role ("View as").
// In that state the live sidebar reflects the previewed role, so editing
// (which uses the REAL role) would clobber the preview and persist a pref
// under a misleading UI — so the editor refuses to edit until preview exits.
function _isPreviewing() {
  try {
    return !!(window.__prismaRealAdmin && sessionStorage.getItem('prisma-view-as'));
  } catch (_) { return false; }
}

// Build editor state from the current effective layout for the REAL user.
export async function initSidebarEditor() {
  const list = document.getElementById('sidebar-editor-list');
  if (!list) return;
  const resetBtnEl = document.getElementById('sidebar-editor-reset');
  if (_isPreviewing()) {
    // Don't edit while previewing a role — would override the live preview.
    list.innerHTML = '<div class="sb-edit-note">You’re previewing another role (View as). '
      + 'Exit the preview (Settings → View as → Reset) to edit your own sidebar.</div>';
    if (resetBtnEl) resetBtnEl.style.display = 'none';
    return;
  }
  if (resetBtnEl) resetBtnEl.style.display = '';
  let role = 'user', privileges = {};
  try {
    const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
    const s = await res.json();
    role = (s && s.role) || 'user';
    privileges = (s && s.privileges) || {};
  } catch (_) {}
  _editorRole = role; _editorPrivs = privileges;
  const savedPref = await loadSavedPref();
  const layout = computeLayout({ role, privileges, savedPref, isPreview: false });
  _editorState = { order: layout.orderedIds.slice(), hidden: new Set(layout.hiddenIds) };
  _renderEditor();
  const resetBtn = document.getElementById('sidebar-editor-reset');
  if (resetBtn && !resetBtn.dataset.bound) {
    resetBtn.dataset.bound = '1';
    resetBtn.addEventListener('click', async () => {
      await savePref({});               // empty pref = role default
      const def = computeLayout({ role: _editorRole, privileges: _editorPrivs, savedPref: null, isPreview: false });
      _editorState = { order: def.orderedIds.slice(), hidden: new Set(def.hiddenIds) };
      _renderEditor();
      applyLayout(def);
    });
  }
}
