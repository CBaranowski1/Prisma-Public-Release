// Attribution-inspired view of Prisma's real context assembly.
// This is deliberately labelled a context circuit: it shows the selected VERA
// profile, routing, and indexed-knowledge associations. It does not claim to
// expose GLM's private chain of thought or raw neural activations.
(function () {
  "use strict";

  var catalog = [];
  var catalogPromise = null;
  var skillData = { skills:[], count:0 };
  var skillsPromise = null;
  var selectedKey = "";
  var searchText = "";
  var activityData = { activities:[], stats:{} };
  var selectedActivityId = "";
  var activitySignature = "";
  var activityTimer = 0;
  var NS = "http://www.w3.org/2000/svg";

  function root() { return document.getElementById("brainCircuit"); }
  function activeProfile() {
    if (window.__getVeraAgent) return window.__getVeraAgent();
    try { return JSON.parse(localStorage.getItem("prisma-vera-agent") || "null"); }
    catch (_) { return null; }
  }
  function currentActivity() {
    var list = activityData.activities || [];
    if (selectedActivityId) {
      var selected = list.filter(function (item) { return item.id === selectedActivityId; })[0];
      if (selected) return selected;
    }
    return list[0] || null;
  }
  function displayProfile() {
    return activeProfile() || (currentActivity() && currentActivity().profile) || null;
  }
  function brainSlug() { return String(window.__BRAIN_ID || "").replace(/^brain:/, ""); }
  function short(value, n) {
    var s = String(value || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function pretty(value) {
    return String(value || "").replace(/^agent:/, "").replace(/^(specialist|reviewer|general)_/, "").replace(/_/g, " ");
  }
  function uniqAgents() {
    var selectedBrain = brainSlug();
    var ordered = catalog.slice().sort(function (a, b) {
      var ap = a.brain === selectedBrain ? 0 : 1;
      var bp = b.brain === selectedBrain ? 0 : 1;
      return ap - bp || String(a.id).localeCompare(String(b.id));
    });
    var seen = Object.create(null);
    return ordered.filter(function (a) {
      if (!a || !a.id || seen[a.id]) return false;
      seen[a.id] = true; return true;
    });
  }
  async function loadCatalog() {
    if (catalogPromise) return catalogPromise;
    catalogPromise = fetch("/api/brain/agents", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function (data) { catalog = Array.isArray(data.agents) ? data.agents : []; return catalog; })
      .catch(function () { catalog = []; return catalog; });
    return catalogPromise;
  }
  async function loadSkills() {
    if (skillsPromise) return skillsPromise;
    skillsPromise = fetch("/api/skills", { credentials:"same-origin", cache:"no-store" })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function (data) { skillData = data || { skills:[], count:0 }; return skillData; })
      .catch(function () { skillData = { skills:[], count:0 }; return skillData; });
    return skillsPromise;
  }
  async function loadActivity(force) {
    try {
      var response = await fetch("/api/brain/activity?limit=16&t=" + Date.now(), { cache:"no-store", credentials:"same-origin" });
      if (!response.ok) throw new Error("http " + response.status);
      var data = await response.json();
      var signature = JSON.stringify((data.activities || []).map(function (item) {
        return [item.id, item.state, item.updated_at, item.feedback, item.reasoning_effective];
      }));
      activityData = data || { activities:[], stats:{} };
      if (force || signature !== activitySignature) {
        activitySignature = signature;
        if (root() && !root().hidden) buildScene();
      }
    } catch (_) {}
  }
  function startActivityPolling() {
    if (activityTimer) clearInterval(activityTimer);
    loadActivity(true);
    activityTimer = setInterval(function () {
      if (root() && !root().hidden) loadActivity(false);
    }, 2500);
  }

  function categoryData() {
    var nodes = (window.__BG && window.__BG.nodes) || [];
    var counts = Object.create(null);
    nodes.forEach(function (node) {
      if (!node || node.type === "agent" || ["conversation", "lesson", "review", "reflection"].indexOf(node.type) >= 0) return;
      var key = String(node.cluster || node.type || "knowledge").toLowerCase();
      counts[key] = (counts[key] || 0) + 1;
    });
    return Object.keys(counts).map(function (key) { return { id:key, title:pretty(key), count:counts[key] }; })
      .sort(function (a, b) { return b.count - a.count || a.id.localeCompare(b.id); });
  }

  function chooseAgents(all, categories) {
    var active = displayProfile();
    var q = searchText.trim().toLowerCase();
    var catNames = categories.map(function (c) { return c.id; });
    function score(agent) {
      var hay = [agent.id, agent.name, agent.squad, agent.role].concat(agent.activation_tokens || []).join(" ").toLowerCase();
      var value = q && hay.indexOf(q) >= 0 ? 1000 : 0;
      (agent.activation_tokens || []).forEach(function (token) {
        var t = String(token).toLowerCase();
        if (catNames.indexOf(t) >= 0) value += 18;
        if (q && (t.indexOf(q) >= 0 || q.indexOf(t) >= 0)) value += 80;
      });
      if (agent.id === "triage") value += 90;
      if (agent.id === "librarian") value += 70;
      if (active && agent.id === active.id && (!active.brain || agent.brain === active.brain)) value += 10000;
      return value;
    }
    var chosen = all.slice().sort(function (a, b) { return score(b) - score(a) || a.id.localeCompare(b.id); });
    if (q) chosen = chosen.filter(function (a) {
      return [a.id, a.name, a.squad, a.role].concat(a.activation_tokens || []).join(" ").toLowerCase().indexOf(q) >= 0 || score(a) >= 10000;
    });
    return chosen.slice(0, 10);
  }

  function chooseCategories(categories, agents) {
    var wanted = Object.create(null);
    agents.forEach(function (agent) {
      (agent.activation_tokens || []).forEach(function (token) { wanted[String(token).toLowerCase()] = true; });
      wanted[String(agent.id || "").replace(/^(specialist|reviewer|general)_/, "").toLowerCase()] = true;
    });
    var q = searchText.trim().toLowerCase();
    return categories.slice().sort(function (a, b) {
      function score(c) { return (wanted[c.id] ? 100000 : 0) + (q && c.id.indexOf(q) >= 0 ? 200000 : 0) + c.count; }
      return score(b) - score(a) || a.id.localeCompare(b.id);
    }).slice(0, 11);
  }

  function svgEl(name, attrs, text) {
    var el = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (key) { el.setAttribute(key, String(attrs[key])); });
    if (text != null) el.textContent = String(text);
    return el;
  }
  function curve(svg, a, b, cls) {
    var dx = Math.max(48, (b.x - (a.x + a.w)) * 0.52);
    var d = "M" + (a.x + a.w) + "," + (a.y + a.h / 2) + " C" + (a.x + a.w + dx) + "," + (a.y + a.h / 2) + " " + (b.x - dx) + "," + (b.y + b.h / 2) + " " + b.x + "," + (b.y + b.h / 2);
    svg.appendChild(svgEl("path", { d:d, "class":"bc-edge " + (cls || "") }));
  }
  function node(svg, item) {
    var group = svgEl("g", { "class":"bc-node " + (item.kind || "") + (item.active ? " active" : ""), tabindex:"0", role:"button", "data-key":item.key });
    group.appendChild(svgEl("rect", { x:item.x, y:item.y, width:item.w, height:item.h, rx:8 }));
    group.appendChild(svgEl("circle", { cx:item.x + 14, cy:item.y + 16, r:4, "class":"bc-node-dot" }));
    group.appendChild(svgEl("text", { x:item.x + 25, y:item.y + 20, "class":"bc-node-title" }, short(item.title, 24)));
    group.appendChild(svgEl("text", { x:item.x + 14, y:item.y + item.h - 11, "class":"bc-node-meta" }, short(item.meta, 29)));
    svg.appendChild(group);
    return group;
  }
  function spread(items, x, top, bottom, w, h, make) {
    var gap = items.length > 1 ? (bottom - top - h) / (items.length - 1) : 0;
    return items.map(function (item, i) { return make(item, { x:x, y:top + gap * i, w:w, h:h }); });
  }

  function clock(ts) {
    if (!ts) return "";
    try { return new Date(ts * 1000).toLocaleTimeString([], { hour:"numeric", minute:"2-digit" }); }
    catch (_) { return ""; }
  }
  function createMetrics(activity) {
    var stats = activityData.stats || {};
    var panel = document.createElement("section"); panel.className = "bc-metrics"; panel.setAttribute("aria-label", "Brain improvement metrics");
    var head = document.createElement("div"); head.className = "bc-metrics-head";
    var dot = document.createElement("span"); dot.className = "bc-live-dot" + (activity && activity.state === "context_ready" ? " running" : "");
    var label = document.createElement("strong"); label.textContent = activity ? (activity.state === "context_ready" ? "LIVE RUN" : "LATEST RUN") : "IMPROVEMENT LOOP";
    head.append(dot, label); panel.appendChild(head);
    var grid = document.createElement("div"); grid.className = "bc-metrics-grid";
    var values = [
      ["retrieval hit", stats.retrieval_rate == null ? "—" : stats.retrieval_rate + "%"],
      ["helpful", stats.helpful_rate == null ? "unrated" : stats.helpful_rate + "%"],
      ["brain recall", activity ? Math.round(activity.brain_latency_ms || 0) + " ms" : "—"],
      ["response", activity && activity.metrics && activity.metrics.response_time ? activity.metrics.response_time + " s" : "—"],
      ["tools", activity ? String(activity.tool_calls || 0) : "—"],
      ["tokens/s", activity && activity.metrics && activity.metrics.tokens_per_second ? String(activity.metrics.tokens_per_second) : "—"],
      ["effort", activity ? (activity.reasoning_effective || (activity.metrics || {}).reasoning_effort || "—") : "—"],
    ];
    values.forEach(function (value) {
      var cell = document.createElement("div"); var b = document.createElement("b"); b.textContent = value[1]; var s = document.createElement("span"); s.textContent = value[0]; cell.append(b, s); grid.appendChild(cell);
    });
    panel.appendChild(grid);
    if (activity) {
      var feedback = document.createElement("div"); feedback.className = "bc-feedback";
      var prompt = document.createElement("span"); prompt.textContent = "Did this retrieval help?"; feedback.appendChild(prompt);
      [["helpful","Helpful"],["missed","Missed context"]].forEach(function (entry) {
        var button = document.createElement("button"); button.type = "button"; button.textContent = entry[1]; button.className = activity.feedback === entry[0] ? "active " + entry[0] : "";
        button.addEventListener("click", function () { submitFeedback(activity.id, activity.feedback === entry[0] ? "clear" : entry[0]); });
        feedback.appendChild(button);
      });
      panel.appendChild(feedback);
    }
    return panel;
  }
  function createTimeline() {
    var line = document.createElement("nav"); line.className = "bc-timeline"; line.setAttribute("aria-label", "Recent brain runs");
    var title = document.createElement("span"); title.className = "bc-timeline-title"; title.textContent = "RECENT CIRCUITS"; line.appendChild(title);
    (activityData.activities || []).slice(0, 8).forEach(function (activity) {
      var button = document.createElement("button"); button.type = "button"; button.className = "bc-run-chip" + ((selectedActivityId || ((activityData.activities || [])[0] || {}).id) === activity.id ? " active" : "");
      var dot = document.createElement("i"); dot.className = activity.state === "context_ready" ? "running" : activity.feedback || "complete";
      var text = document.createElement("span"); text.textContent = short(activity.query || "Untitled request", 26);
      var t = document.createElement("time"); t.textContent = clock(activity.started_at);
      button.append(dot, text, t);
      button.addEventListener("click", function () { selectedActivityId = activity.id; selectedKey = ""; buildScene(); });
      line.appendChild(button);
    });
    return line;
  }
  async function submitFeedback(activityId, feedback) {
    try {
      var response = await fetch("/api/brain/activity/" + encodeURIComponent(activityId) + "/feedback", {
        method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body:JSON.stringify({feedback:feedback}),
      });
      if (!response.ok) throw new Error("http " + response.status);
      await loadActivity(true);
    } catch (_) {}
  }

  function buildScene() {
    var host = root(); if (!host) return;
    var restoreSearchFocus = document.activeElement && document.activeElement.classList && document.activeElement.classList.contains("bc-search");
    var allAgents = uniqAgents();
    var allCats = categoryData();
    var agents = chooseAgents(allAgents, allCats);
    var categories = chooseCategories(allCats, agents);
    var activity = currentActivity();
    var active = displayProfile();
    var retrievals = activity && Array.isArray(activity.retrievals) ? activity.retrievals : [];
    var retrievedCategories = Object.create(null);
    retrievals.forEach(function (item) { if (item.category) retrievedCategories[String(item.category).toLowerCase()] = true; });
    host.innerHTML = "";

    var board = document.createElement("div"); board.className = "bc-board";
    var top = document.createElement("div"); top.className = "bc-topline";
    var title = document.createElement("strong"); title.textContent = activity && activity.state === "context_ready" ? "Live context circuit · processing" : "Live context circuit";
    var note = document.createElement("span"); note.textContent = "Measured effort routing → skills/profile selection → retrieved evidence → synthesis → response gate. Context telemetry, not hidden chain of thought.";
    top.append(title, note);
    var search = document.createElement("input"); search.className = "bc-search"; search.type = "search"; search.placeholder = "Trace a profile or concept…"; search.value = searchText; search.setAttribute("aria-label", "Trace a profile or concept");
    var timer = 0; search.addEventListener("input", function () { searchText = search.value; clearTimeout(timer); timer = setTimeout(buildScene, 140); });
    var inspector = document.createElement("aside"); inspector.className = "bc-inspector"; inspector.hidden = true;
    var svg = svgEl("svg", { viewBox:"0 0 1600 780", preserveAspectRatio:"xMidYMid meet", "class":"bc-svg", "aria-label":"Prisma context circuit" });
    var defs = svgEl("defs");
    var pattern = svgEl("pattern", { id:"bcGrid", width:24, height:24, patternUnits:"userSpaceOnUse" });
    pattern.appendChild(svgEl("path", { d:"M 24 0 L 0 0 0 24", fill:"none", stroke:"#203044", "stroke-width":"0.6" }));
    defs.appendChild(pattern); svg.appendChild(defs);
    svg.appendChild(svgEl("rect", { width:1600, height:780, fill:"url(#bcGrid)", "class":"bc-grid" }));

    ["INPUT", "EFFORT", "ROUTER", "SKILLS / VERA", "KNOWLEDGE", "RETRIEVAL", "MODEL", "VERIFY"].forEach(function (label, i) {
      svg.appendChild(svgEl("text", { x:[24,170,335,505,725,960,1175,1375][i], y:92, "class":"bc-layer-label" }, label));
    });

    var items = [];
    var inputTitle = searchText || (activity && activity.query) || "Current request";
    var input = { key:"input", kind:"input", title:short(inputTitle, 22), meta:activity ? clock(activity.started_at) + " · " + (activity.agent_mode ? "agent" : "chat") : "user + conversation context", x:20, y:337, w:130, h:64 };
    var effortValue = activity ? (activity.reasoning_effective || (activity.metrics || {}).reasoning_effort || "default") : "auto / low / high / max";
    var effort = { key:"effort", kind:"router", title:"Reasoning " + effortValue, meta:activity ? (activity.reasoning_route || "per-turn budget") : "adaptive compute budget", x:165, y:337, w:145, h:64, active:!!activity };
    var routeMeta = activity && activity.profile ? (activity.profile_auto ? "auto-routed · " : "selected · ") + pretty(activity.profile.id) : "intent + profile routing";
    var router = { key:"router", kind:"router", title:"Intent + VERA", meta:routeMeta, x:330, y:337, w:145, h:64 };
    var activityMetrics = activity && activity.metrics ? activity.metrics : {};
    var groundingStatus = activityMetrics.grounding_status || "none";
    var groundingMeta = groundingStatus === "grounded"
      ? "grounded · " + Number(activityMetrics.cited_source_count || 0) + " cited"
      : groundingStatus === "partial"
        ? "partial · " + Number(activityMetrics.cited_source_count || 0) + " cited"
        : Number(activityMetrics.evidence_count || 0) > 0 ? "evidence available · uncited" : "no cited evidence";
    var synthesis = { key:"synthesis", kind:"synthesis", title:"GLM synthesis", meta:activity && activity.state === "context_ready" ? "generating now" : groundingMeta, x:1170, y:337, w:160, h:64, active:!!activity };
    var verifier = { key:"verifier", kind:"review", title:"Response gate", meta:activity && activity.tool_calls ? "tool results + syntax checks" : "policy + format checks", x:1370, y:337, w:180, h:64, active:!!activity };
    items.push(input, effort, router, synthesis, verifier);
    var agentItems = spread(agents, 500, 112, 702, 180, 48, function (agent, box) {
      var isActive = !!active && agent.id === active.id && (!active.brain || agent.brain === active.brain);
      var item = Object.assign(box, { key:"agent:" + agent.brain + ":" + agent.id, kind:(agent.role === "reviewer" ? "review" : "agent"), title:pretty(agent.id), meta:(agent.role || "profile") + " · " + (agent.squad || "general"), active:isActive, data:agent });
      items.push(item); return item;
    });
    var catItems = spread(categories, 720, 112, 702, 190, 48, function (cat, box) {
      var item = Object.assign(box, { key:"category:" + cat.id, kind:"category", title:cat.title, meta:cat.count + " visible notes", active:!!retrievedCategories[cat.id], data:cat }); items.push(item); return item;
    });
    var evidenceSource = retrievals.length ? retrievals.slice(0, 6) : [{ id:"vector", category:"", source:"", score:0, characters:0, fallback:true }];
    var evidenceItems = spread(evidenceSource, 955, retrievals.length > 1 ? 205 : 337, retrievals.length > 1 ? 575 : 401, 170, 56, function (evidence, box) {
      var provider = String(evidence.provider_name || evidence.provider || "Vector retrieval").replace(/^Brain:\s*/i, "");
      var title = evidence.fallback ? "Vector retrieval" : (evidence.category ? pretty(evidence.category) : short(evidence.id || "Retrieved note", 21));
      var meta = evidence.fallback ? ((window.__BG && window.__BG.stats && window.__BG.stats.nodes || 0) + " indexed nodes") : (short(provider, 17) + (evidence.score ? " · " + Number(evidence.score).toFixed(2) : ""));
      var item = Object.assign(box, { key:"evidence:" + (evidence.id || "vector"), kind:"memory", title:title, meta:meta, active:!!activity && !evidence.fallback, data:evidence }); items.push(item); return item;
    });

    curve(svg, input, effort, activity ? "active" : "");
    curve(svg, effort, router, activity ? "active" : "");
    agentItems.forEach(function (agentItem) {
      var edgeActive = agentItem.active;
      curve(svg, router, agentItem, edgeActive ? "active" : (agentItem.kind === "review" ? "review" : ""));
      var tokens = (agentItem.data.activation_tokens || []).map(function (v) { return String(v).toLowerCase(); });
      var base = String(agentItem.data.id || "").replace(/^(specialist|reviewer|general)_/, "").toLowerCase();
      var matches = catItems.filter(function (catItem) { return tokens.indexOf(catItem.data.id) >= 0 || catItem.data.id === base; });
      if (!matches.length) matches = catItems.slice(0, agentItem.active ? 3 : 1);
      matches.slice(0, agentItem.active ? 5 : 2).forEach(function (catItem) { curve(svg, agentItem, catItem, agentItem.active ? "active" : ""); });
      if (agentItem.kind === "review") curve(svg, agentItem, synthesis, agentItem.active ? "active review" : "review");
    });
    catItems.forEach(function (catItem) {
      evidenceItems.forEach(function (evidenceItem) {
        var match = !retrievals.length || !evidenceItem.data.category || String(evidenceItem.data.category).toLowerCase() === catItem.data.id;
        if (match) curve(svg, catItem, evidenceItem, catItem.active ? "active" : "");
      });
    });
    evidenceItems.forEach(function (evidenceItem) { curve(svg, evidenceItem, synthesis, evidenceItem.active ? "active" : ""); });
    curve(svg, synthesis, verifier, activity ? "active review" : "review");

    items.forEach(function (item) {
      var g = node(svg, item);
      function pick() { selectedKey = item.key; showInspector(inspector, item); }
      g.addEventListener("click", pick);
      g.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); } });
    });

    board.appendChild(svg); host.append(board, top, search, createMetrics(activity), createTimeline(), inspector);
    var stat = document.getElementById("graphStat");
    if (stat) stat.textContent = (activity && activity.state === "context_ready" ? "LIVE · " : "") + Number(skillData.count || (skillData.skills || []).length || 0) + " skills · " + allAgents.length + " profiles · " + retrievals.length + " retrieved · " + allCats.length + " features";
    if (selectedKey) {
      var selected = items.filter(function (item) { return item.key === selectedKey; })[0];
      if (selected) showInspector(inspector, selected);
    } else if (active) {
      var activeItem = agentItems.filter(function (item) { return item.active; })[0];
      if (activeItem) showInspector(inspector, activeItem);
    }
    if (restoreSearchFocus) setTimeout(function () { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }, 0);
  }

  function showInspector(host, item) {
    host.hidden = false; host.innerHTML = "";
    var h = document.createElement("h3"); h.textContent = item.title;
    var p = document.createElement("p"); p.textContent = item.meta || "";
    host.append(h, p);
    if (item.data && item.key.indexOf("agent:") === 0) {
      var desc = document.createElement("p"); desc.textContent = "This recovered VERA profile specializes the live chat system prompt. Its tool declarations remain subject to Prisma's normal permissions and approvals."; host.appendChild(desc);
      var tags = document.createElement("div"); tags.className = "bc-inspector-tags";
      (item.data.activation_tokens || []).slice(0, 10).forEach(function (token) { var s = document.createElement("span"); s.textContent = token; tags.appendChild(s); });
      host.appendChild(tags);
      var active = activeProfile();
      var isActive = !!active && active.id === item.data.id && active.brain === item.data.brain;
      var button = document.createElement("button"); button.type = "button"; button.className = isActive ? "active" : ""; button.textContent = isActive ? "Deactivate profile" : "Activate in chat";
      button.addEventListener("click", function () {
        if (window.__setVeraAgent) window.__setVeraAgent(isActive ? null : item.data);
        buildScene();
      });
      host.appendChild(button);
    } else if (item.key.indexOf("category:") === 0) {
      var cp = document.createElement("p"); cp.textContent = "Indexed feature cluster used by brain retrieval and associated VERA specialists."; host.appendChild(cp);
    } else if (item.key.indexOf("evidence:") === 0 && item.data) {
      var ed = document.createElement("p");
      ed.textContent = item.data.fallback
        ? "The vector index is ready. A completed chat trace will replace this node with the exact evidence that entered the prompt."
        : "Retrieved from " + (item.data.provider_name || item.data.provider || "the brain") + " · score " + Number(item.data.score || 0).toFixed(3) + " · " + Number(item.data.characters || 0) + " prompt characters.";
      host.appendChild(ed);
      if (item.data.source) { var src = document.createElement("p"); src.textContent = "Source: " + item.data.source; host.appendChild(src); }
    } else {
      var explanations = {
        input:"The live request and retained conversation context enter here.",
        effort:"Prisma deterministically selects low or high compute in Auto mode. Max is explicit-only on one Spark to prevent accidental latency spikes.",
        router:"VERA triage matches intent to a selected or relevant specialist profile.",
        memory:"Prisma retrieves a bounded set of notes from the selected brain provider.",
        synthesis:"GLM-5.3 Flash combines the request, active profile, owner-scoped skills, tools, and retrieved evidence into the answer.",
        verifier:"Prisma applies its real response, policy, tool-result, and gateable-code checks. This is not a claim that every factual statement has been independently proven."
      };
      var ep = document.createElement("p"); ep.textContent = explanations[item.key] || "Context-processing stage."; host.appendChild(ep);
    }
  }

  window.__graphShowCircuit = function () {
    Promise.all([loadCatalog(), loadSkills()]).then(function () { buildScene(); startActivityPolling(); });
  };
  window.__brainCircuitFit = function () { var board = root() && root().querySelector(".bc-board"); if (board) board.scrollTo({ left:0, top:0, behavior:"smooth" }); };
  window.addEventListener("prisma:vera-agent-changed", function () { if (root() && !root().hidden) buildScene(); });
})();
