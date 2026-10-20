// Neural Brain overlay: one live graph powers an attribution-inspired context
// circuit (default), the existing 3D galaxy, and the compact 2D map.
(function () {
  "use strict";
  var mode = "circuit";
  function ov() { return document.getElementById("brain-graph-overlay"); }
  function stat(t) { var s = document.getElementById("graphStat"); if (s) s.textContent = t; }

  // The brain to graph = whatever's selected in the Brain provider dropdown, so
  // switching brains and reopening the graph regenerates it for that brain.
  function selectedBrain() {
    var sel = document.getElementById("brain-provider");
    // The provider list is loaded asynchronously when the Brain view opens.
    // A fast Graph click must not fall through to the first discovered brain
    // (the small conversation-evals store); the full Council is the canonical
    // visual brain for this Prisma build.
    return (sel && sel.value) ? sel.value : "brain:llm-council";
  }
  var _bgCache = {};   // brain id -> parsed graph data (avoid re-fetch/re-parse of a big payload on reopen)
  async function fetchGraph() {
    var bid = selectedBrain();
    window.__BRAIN_ID = bid;   // so node-click note fetches read the same brain
    if (_bgCache[bid]) return _bgCache[bid];
    var url = "/api/brain-graph?visual=true&t=" + Date.now() + (bid ? "&brain=" + encodeURIComponent(bid) : "");
    var r = await fetch(url, { cache: "no-store", credentials: "same-origin" });
    if (!r.ok) throw new Error("http " + r.status);
    var data = await r.json();
    _bgCache[bid] = data;
    return data;
  }
  function syncModeTabs() {
    document.querySelectorAll("[data-brain-mode]").forEach(function (button) {
      var active = button.getAttribute("data-brain-mode") === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    var signals = document.getElementById("graph-legend-toggle");
    if (signals) signals.textContent = mode === "circuit" ? "Signals" : "Legend";
  }
  // Show/hide the 3D-only chrome. Critical: #graph3dLabels are absolutely-
  // positioned HTML spans at fixed screen coords — if left visible in 2D they
  // stay screen-locked over the 2D canvas (the reported "stuck labels" bug).
  // The HUD vignette + flight-controls pill are 3D-only too.
  function setChrome3D(show) {
    ["graph3dCanvas", "graph3dLabels", "graph3dHud", "graph3dControls"].forEach(function (id) {
      var e = document.getElementById(id); if (e) e.style.display = show ? "" : "none";
    });
    if (!show) { var d = document.getElementById("graph3dDetail"); if (d) d.style.display = "none"; }
  }
  function show3D() {
    mode = "3d";
    var cc = document.getElementById("brainCircuit"); if (cc) { cc.hidden = true; cc.style.display = "none"; }
    var c2 = document.getElementById("graphCanvas"); if (c2) c2.style.display = "none";
    // Re-show the 3D chrome (a prior 2D toggle hid it; graph-3d's __graphShow
    // only un-hides on first create, so 2D->reopen would otherwise render hidden).
    setChrome3D(true);
    if (window.__graphShow) window.__graphShow();
    syncModeTabs();
  }
  function show2D() {
    mode = "2d";
    setChrome3D(false);
    var cc = document.getElementById("brainCircuit"); if (cc) { cc.hidden = true; cc.style.display = "none"; }
    var c2 = document.getElementById("graphCanvas"); if (c2) c2.style.display = "";
    if (window.__graphShow2D) window.__graphShow2D();
    syncModeTabs();
  }
  function showCircuit() {
    mode = "circuit";
    setChrome3D(false);
    var c2 = document.getElementById("graphCanvas"); if (c2) c2.style.display = "none";
    var cc = document.getElementById("brainCircuit"); if (cc) { cc.hidden = false; cc.style.display = ""; }
    if (window.__graphShowCircuit) window.__graphShowCircuit();
    syncModeTabs();
  }
  function setMode(next) {
    if (next === "3d") show3D();
    else if (next === "2d") show2D();
    else showCircuit();
  }

  async function open() {
    var el = ov(); if (!el) return;
    el.hidden = false; el.style.display = "";
    stat("loading…");
    try {
      var data = await fetchGraph();
      window.__BG = data; window.__BRAIN_DATA__ = data;
      if (!data || !data.nodes || !data.nodes.length) {
        stat((data && data.stats && data.stats.error) || "no brain vault configured");
        return;
      }
      showCircuit(); // default: meaningful routing/retrieval circuit
    } catch (e) {
      try { console.error("Brain graph failed to load:", e); } catch (_) {}
      stat("failed to load graph" + (e && e.message ? " (" + e.message + ")" : ""));
    }
  }
  function close() { var el = ov(); if (el) { el.hidden = true; el.style.display = "none"; } }

  window.openBrainGraph = open;
  window.closeBrainGraph = close;
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { var el = ov(); if (el && !el.hidden) close(); }
  });
  function fit() {
    if (mode === "3d") { if (window.__graph3dReset) window.__graph3dReset(); }
    else if (mode === "2d") { if (window.__graphResetView) window.__graphResetView(); }
    else if (window.__brainCircuitFit) window.__brainCircuitFit();
  }
  function toggleLegend() {
    if (mode === "circuit") {
      var panels = document.querySelectorAll("#brainCircuit .bc-metrics, #brainCircuit .bc-timeline");
      if (!panels.length) return;
      var show = panels[0].hasAttribute("hidden");
      panels.forEach(function (panel) { if (show) panel.removeAttribute("hidden"); else panel.setAttribute("hidden", ""); });
      var signalButton = document.getElementById("graph-legend-toggle");
      if (signalButton) signalButton.classList.toggle("active", show);
      return;
    }
    var lg = document.getElementById("graph3dAgents"); // legend panel (built by graph-3d on first 3D show)
    if (!lg) return;
    var hidden = lg.hasAttribute("hidden");
    if (hidden) lg.removeAttribute("hidden"); else lg.setAttribute("hidden", "");
    var b = document.getElementById("graph-legend-toggle");
    if (b) b.classList.toggle("active", hidden);
  }
  function wire() {
    var g = document.getElementById("brain-open-graph-btn"); if (g) g.addEventListener("click", open);
    document.querySelectorAll("[data-brain-mode]").forEach(function (button) {
      button.addEventListener("click", function () { setMode(button.getAttribute("data-brain-mode")); });
    });
    var x = document.getElementById("graph-close"); if (x) x.addEventListener("click", close);
    var f = document.getElementById("graphReset"); if (f) f.addEventListener("click", fit);
    var lt = document.getElementById("graph-legend-toggle"); if (lt) lt.addEventListener("click", toggleLegend);
    var bn = document.getElementById("graph-browse-notes");
    if (bn) bn.addEventListener("click", function () {
      close();                                   // reveal the brain list/ingest pane behind
      if (window.showView) window.showView("brain");
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
