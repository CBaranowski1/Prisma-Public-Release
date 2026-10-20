// uiScale.js — whole-UI display-size (zoom) scaling, per-user.
// Mirrors theme.js: a localStorage cache for instant / no-flash apply on load,
// plus per-user sync to the server via the generic /api/prefs/{key} route.
// The graph canvas maps clicks rect-relative (e.clientX - rect.left), and both
// clientX and getBoundingClientRect() are in the same zoom-aware CSS-pixel
// space, so CSS `zoom` is transparent to it — no carve-out needed.
(function () {
  "use strict";
  var LS_KEY = "prisma-ui-scale";
  var MIN = 0.9, MAX = 1.6;

  function clamp(n) {
    n = parseFloat(n);
    if (!n || isNaN(n)) return 1;
    return Math.max(MIN, Math.min(MAX, n));
  }
  function getScale() { return clamp(localStorage.getItem(LS_KEY) || 1); }
  function apply(scale) {
    document.documentElement.style.setProperty("--ui-zoom", String(clamp(scale)));
  }

  function _persist(scale) {
    scale = clamp(scale);
    try { localStorage.setItem(LS_KEY, String(scale)); } catch (e) {}
    try {
      fetch("/api/prefs/ui-scale", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ value: scale }),
      }).catch(function () {});
    } catch (e) {}
  }

  var _saveTimer = null;
  function _debouncedPersist(scale) {
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(function () { _persist(scale); }, 350);
  }

  function syncControls(scale) {
    scale = clamp(scale);
    var pct = Math.round(scale * 100);
    var slider = document.getElementById("display-size-slider");
    var label = document.getElementById("display-size-value");
    if (slider) slider.value = String(pct);
    if (label) label.textContent = pct + "%";
    var btns = document.querySelectorAll("[data-display-size]");
    for (var i = 0; i < btns.length; i++) {
      var on = Math.abs(parseFloat(btns[i].dataset.displaySize) - scale) < 0.001;
      btns[i].classList.toggle("active", on);
    }
  }

  // Changing `zoom` reflows the page; some scroll containers (the sidebar's
  // inner list) can momentarily keep a stale scroll offset, briefly clipping
  // their top. Nudge layout observers on the next frame so everything settles.
  function _nudgeReflow() {
    requestAnimationFrame(function () {
      try { window.dispatchEvent(new Event("resize")); } catch (e) {}
    });
  }

  // Set the scale: apply live, reflect in the controls, persist (debounced).
  function set(scale, opts) {
    scale = clamp(scale);
    apply(scale);
    syncControls(scale);
    _nudgeReflow();
    if (!opts || !opts.noSave) _debouncedPersist(scale);
  }

  async function loadFromServer() {
    try {
      var res = await fetch("/api/prefs/ui-scale", { credentials: "same-origin" });
      if (!res.ok) return;
      var data = await res.json();
      if (data && data.value != null) {
        var s = clamp(data.value);
        apply(s);
        try { localStorage.setItem(LS_KEY, String(s)); } catch (e) {}
        syncControls(s);
        _nudgeReflow();
      }
    } catch (e) {}
  }

  function initControls() {
    var slider = document.getElementById("display-size-slider");
    if (slider && !slider.dataset.bound) {
      slider.dataset.bound = "1";
      slider.addEventListener("input", function () {
        set(parseInt(slider.value, 10) / 100);
      });
    }
    var btns = document.querySelectorAll("[data-display-size]");
    for (var i = 0; i < btns.length; i++) {
      (function (b) {
        if (b.dataset.bound) return;
        b.dataset.bound = "1";
        b.addEventListener("click", function () { set(parseFloat(b.dataset.displaySize)); });
      })(btns[i]);
    }
    syncControls(getScale());
  }

  function boot() {
    apply(getScale());   // ensure applied from cache (the head script also does this)
    initControls();
    loadFromServer();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.uiScale = { apply: apply, getScale: getScale, set: set };
})();
