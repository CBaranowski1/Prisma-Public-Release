// static/js/brain.js — Brain tab: search external knowledge provider
// ES6 module; also bound as window.brainModule for the view router.

const API = window.location.origin;
let provider = null;
let loaded = false;
let agentCatalog = [];
let activeAgentMemory = null;

function brainSlug() {
  return String(provider || '').replace(/^brain:/, '');
}

function activeAgent() {
  try {
    const stored = JSON.parse(localStorage.getItem('prisma-vera-agent') || 'null');
    // Once the user explicitly selects a profile, keep that in-memory value
    // authoritative for this page. Some privacy modes allow storage reads but
    // reject writes; re-reading a stale stored value would otherwise undo the
    // click immediately.
    if (!activeAgentMemory && stored) activeAgentMemory = stored;
  } catch (_) {}
  return activeAgentMemory || window.__prismaVeraAgent || null;
}

function setActiveAgent(agent) {
  const selected = agent ? {
    id: agent.id, brain: agent.brain, name: agent.name,
    squad: agent.squad, role: agent.role,
  } : null;
  activeAgentMemory = selected;
  window.__prismaVeraAgent = selected;
  try {
    if (selected) localStorage.setItem('prisma-vera-agent', JSON.stringify(selected));
    else localStorage.removeItem('prisma-vera-agent');
  } catch (_) {}
  window.dispatchEvent(new CustomEvent('prisma:vera-agent-changed', { detail: selected }));
  renderAgents();
}

function preferredAgents() {
  const selected = brainSlug();
  const ordered = agentCatalog.slice().sort((a, b) => {
    const ap = a.brain === selected ? 0 : 1;
    const bp = b.brain === selected ? 0 : 1;
    return ap - bp || String(a.id).localeCompare(String(b.id));
  });
  const seen = new Set();
  return ordered.filter(a => {
    if (!a || !a.id || seen.has(a.id)) return false;
    seen.add(a.id); return true;
  });
}

function renderActiveAgent() {
  const host = document.getElementById('brain-agent-active');
  if (!host) return;
  const active = activeAgent();
  host.innerHTML = '';
  host.hidden = !active;
  if (!active) return;
  const pulse = document.createElement('span'); pulse.className = 'brain-agent-pulse'; pulse.setAttribute('aria-hidden', 'true');
  const copy = document.createElement('div'); copy.className = 'brain-agent-active-copy';
  const label = document.createElement('strong'); label.textContent = active.name || active.id;
  const meta = document.createElement('span'); meta.textContent = `${active.role || 'profile'} · ${active.squad || 'general'} · active in chat`;
  copy.append(label, meta);
  const graph = document.createElement('button'); graph.type = 'button'; graph.className = 'brain-agent-action'; graph.textContent = 'Trace';
  graph.addEventListener('click', () => { if (window.openBrainGraph) window.openBrainGraph(); });
  const off = document.createElement('button'); off.type = 'button'; off.className = 'brain-agent-action'; off.textContent = 'Deactivate';
  off.addEventListener('click', () => setActiveAgent(null));
  host.append(pulse, copy, graph, off);
}

function renderAgents() {
  const host = document.getElementById('brain-agent-list');
  const count = document.getElementById('brain-agent-count');
  if (!host) return;
  const all = preferredAgents();
  const sourceCount = agentCatalog.length;
  if (count) count.textContent = `${all.length} profiles · ${sourceCount} sources`;
  const q = String((document.getElementById('brain-agent-search') || {}).value || '').trim().toLowerCase();
  const shown = all.filter(a => !q || [a.id, a.name, a.squad, a.role, ...(a.activation_tokens || [])]
    .join(' ').toLowerCase().includes(q));
  const active = activeAgent();
  host.innerHTML = '';
  shown.forEach(agent => {
    const card = document.createElement('button'); card.type = 'button'; card.className = 'brain-agent-card';
    const isActive = !!active && active.id === agent.id && active.brain === agent.brain;
    card.classList.toggle('active', isActive);
    card.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    const top = document.createElement('span'); top.className = 'brain-agent-card-top';
    const name = document.createElement('strong'); name.textContent = agent.name || agent.id;
    const squad = document.createElement('em'); squad.textContent = agent.squad || 'general';
    top.append(name, squad);
    const role = document.createElement('span'); role.className = 'brain-agent-card-meta'; role.textContent = `${agent.role || 'profile'} · ${agent.brain || ''}`;
    const tokens = document.createElement('span'); tokens.className = 'brain-agent-card-tokens'; tokens.textContent = (agent.activation_tokens || []).slice(0, 5).join(' · ');
    card.append(top, role, tokens);
    card.addEventListener('click', () => setActiveAgent(isActive ? null : agent));
    host.appendChild(card);
  });
  if (!shown.length) {
    const empty = document.createElement('div'); empty.className = 'brain-agent-none'; empty.textContent = 'No VERA profiles match that search.'; host.appendChild(empty);
  }
  renderActiveAgent();
}

async function loadAgents() {
  try {
    const data = await (await fetch(`${API}/api/brain/agents`, {credentials:'same-origin'})).json();
    agentCatalog = Array.isArray(data.agents) ? data.agents : [];
  } catch (_) { agentCatalog = []; }
  renderAgents();
}

async function loadProviders() {
  let ps = [];
  try { ps = await (await fetch(`${API}/api/brain/providers`, {credentials:'same-origin'})).json(); } catch(e){ ps = []; }
  const sel = document.getElementById('brain-provider'); if(!sel) return;
  const external = (ps||[]).filter(p => p.provider_id !== 'native');
  sel.innerHTML = '';
  if (!external.length) {
    const o=document.createElement('option'); o.value=''; o.textContent='No brain configured'; sel.appendChild(o);
    provider=null; return;
  }
  external.forEach(p => { const o=document.createElement('option'); o.value=p.provider_id;
    o.textContent=p.display_name + (p.healthy===false?' (unavailable)':'') + (p.enabled===false?' (disabled)':'');
    sel.appendChild(o); });
  const available = new Set(external.map(p => p.provider_id));
  let saved = '';
  try { saved = localStorage.getItem('prisma-brain-provider') || ''; } catch(e) {}
  const preferred = available.has(saved) ? saved
    : (available.has('brain:llm-council') ? 'brain:llm-council' : external[0].provider_id);
  sel.value = preferred;
  provider = preferred;
}

async function loadCats() {
  const row=document.getElementById('brain-cats'); if(!row) return; row.innerHTML='';
  if(!provider) return;
  let cats=[];
  try { cats = await (await fetch(`${API}/api/brain/categories?provider=${encodeURIComponent(provider)}`,{credentials:'same-origin'})).json(); } catch(e){ cats=[]; }
  ['All', ...cats].forEach((c,i)=>{ const b=document.createElement('button'); b.type='button';
    b.className='brain-cat'+(i===0?' active':''); b.textContent=c;
    b.addEventListener('click', ()=>{ row.querySelectorAll('.brain-cat').forEach(x=>x.classList.remove('active')); b.classList.add('active'); search(); });
    row.appendChild(b); });
}

async function search() {
  const grid=document.getElementById('brain-results'); const empty=document.getElementById('brain-empty');
  if(!grid) return;
  if(!provider){ grid.innerHTML=''; if(empty){empty.hidden=false; empty.textContent='No brain provider configured. Add one in Settings → Brain.';} return; }
  const q=(document.getElementById('brain-search')||{}).value||'';
  const catBtn=document.querySelector('#brain-cats .brain-cat.active');
  let url=`${API}/api/brain/search?provider=${encodeURIComponent(provider)}&q=${encodeURIComponent(q)}`;
  if(catBtn && catBtn.textContent!=='All') url+=`&category=${encodeURIComponent(catBtn.textContent)}`;
  let docs=[];
  try { docs = await (await fetch(url,{credentials:'same-origin'})).json(); } catch(e){ docs=[]; }
  grid.innerHTML='';
  if(!docs.length){ if(empty){empty.hidden=false; empty.textContent = q? 'No matches.' : 'Type to search, or browse by category.';} return; }
  if(empty) empty.hidden=true;
  docs.forEach(d=>{ const c=document.createElement('div'); c.className='brain-card glass';
    const t=document.createElement('div'); t.className='brain-card-title'; t.textContent=d.title||'(untitled)';
    const cat=document.createElement('div'); cat.className='brain-card-cat'; cat.textContent=d.category||'';
    const p=document.createElement('p'); p.className='brain-card-snippet'; p.textContent=d.snippet||'';
    c.append(t,cat,p);
    if(d.kind==='vera_profile'){
      const action=document.createElement('span'); action.className='brain-card-action'; action.textContent='Activate profile';
      c.appendChild(action); c.classList.add('brain-card-interactive'); c.tabIndex=0; c.setAttribute('role','button');
      const activate=()=>{
        const agent=agentCatalog.find(a=>a.id===d.agent_id && a.brain===d.source_brain)
          || agentCatalog.find(a=>a.id===d.agent_id);
        if(!agent) return;
        setActiveAgent(agent);
        action.textContent='Active in chat'; c.classList.add('active');
      };
      c.addEventListener('click',activate);
      c.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); activate(); } });
      const active=activeAgent();
      if(active && active.id===d.agent_id && active.brain===d.source_brain){ action.textContent='Active in chat'; c.classList.add('active'); }
    }
    grid.appendChild(c); });
}

function onShow(){ if(!loaded){ loaded=true; loadProviders().then(()=>Promise.all([loadCats().then(search), loadAgents()])); } else { renderAgents(); } }

function init(){
  const s=document.getElementById('brain-search');
  if(s) s.addEventListener('input', ()=>{ clearTimeout(window._brainT); window._brainT=setTimeout(search,250); });
  const sel=document.getElementById('brain-provider');
  if(sel) sel.addEventListener('change', ()=>{
    provider=sel.value||null;
    try { if(provider) localStorage.setItem('prisma-brain-provider', provider); } catch(e) {}
    loadCats().then(search); renderAgents();
  });
  const as=document.getElementById('brain-agent-search');
  if(as) as.addEventListener('input', ()=>{ clearTimeout(window._brainAgentT); window._brainAgentT=setTimeout(renderAgents,120); });
  window.addEventListener('prisma:vera-agent-changed', renderAgents);
}

const brainModule={init,onShow,refresh:()=>{loaded=false;onShow();}};
export default brainModule;
window.brainModule=brainModule;
window.__setVeraAgent = setActiveAgent;
window.__getVeraAgent = activeAgent;
