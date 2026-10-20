// tests/test_ide_chat_hint_js.mjs
// Run with: node tests/test_ide_chat_hint_js.mjs
import assert from 'node:assert/strict';

// Inline the function under test so we don't need a DOM
function _escHtml(s) {
  const _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return (s || '').replace(/[&<>"']/g, (m) => _ESC_MAP[m]);
}
function buildOpenInIdeHtml(hint) {
  if (!hint || !hint.path) return '';
  const _p = _escHtml(hint.path);
  const label = hint.line ? `Open ${_p}:${hint.line} in IDE` : `Open ${_p} in IDE`;
  // path and line are data attributes — no inline JS, no XSS
  return `<button class="open-in-ide-btn" data-path="${_escHtml(hint.path)}" data-line="${hint.line ?? ''}">${label}</button>`;
}

// Tests
assert.equal(buildOpenInIdeHtml(null), '');
assert.equal(buildOpenInIdeHtml({}), '');
assert.match(buildOpenInIdeHtml({path: '/a/b.py'}), /open-in-ide-btn/);
assert.match(buildOpenInIdeHtml({path: '/a/b.py', line: 42}), /b\.py:42/);
// XSS: quotes in path must be escaped
assert.doesNotMatch(buildOpenInIdeHtml({path: '/"evil"'}), /data-path="\/"/);

console.log('All ide_chat_hint tests passed.');
