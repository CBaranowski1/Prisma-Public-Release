/* In-page port of the council's brain.js — EXACT radial layout: offline-brain core
   in the centre, agents radiating around it (sorted: triage, reviewers, specialists,
   overseers/chief), each agent's own knowledge as a two-column tendril streak.
   No iframe, no fetch — reads window.__BG, draws into #graphCanvas inside #graphWrap. */
"use strict";
(function(){
  var PI=Math.PI;
  function esc(s){return String(s).replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}
  var COLORS={concept:"#9b7cff",pack:"#5b9dff",ftz:"#46c7c7",book:"#e07b53",trade:"#d65db1",recipe:"#5bd1a0",security:"#ee5a6f",maintainability:"#5ec2e0",clarity:"#9fe870",prompting:"#c77dff",triage:"#ffa94d",correctness:"#ff5252",performance:"#4dabf7",redundancy:"#b8a89a",synthesis:"#f2cd60",overseer_eng:"#7ee0c0",overseer_risk:"#ffb27a",web:"#61dafb",bash:"#89e051",abap:"#0a6ed1",oracle:"#c74634",ruby:"#cc342d",postgres:"#336791",python:"#ffd43b",codegen:"#ff7eb6",access:"#a4373a",domain:"#46c7c7",imported:"#7f8fa6",conversation:"#41c980",lesson:"#e0a93b",review:"#8a97ad",database:"#7aa2f7",erp:"#0faf9a",wms:"#f4a261",testing:"#63e6be",api:"#74c0fc",observability:"#ffd43b",documentation:"#b0c4de",agentsdk:"#da77f2",java:"#e76f00",csharp:"#9b4f96",go:"#00add8",rust:"#dea584",cpp:"#00599c",php:"#777bb4",tools:"#ffd166",frontend:"#ff6b9d",devops:"#5eead4",mobile:"#a78bfa",dataeng:"#fb923c",product:"#f0abfc",ux:"#67e8f9",architecture:"#fcd34d",backend:"#86efac",agent:"#ffffff"};
  var RAD={concept:4,pack:5,ftz:4,book:4,trade:4,recipe:4,security:5,maintainability:5,clarity:5,prompting:5,triage:5,correctness:5,performance:5,redundancy:4,synthesis:5,overseer_eng:5,overseer_risk:5,web:5,bash:5,abap:5,oracle:5,ruby:5,postgres:5,python:5,codegen:4,access:4,domain:5,imported:4,conversation:4,lesson:4,review:3,database:5,erp:5,wms:5,testing:5,api:5,observability:5,documentation:5,agentsdk:4,java:5,csharp:5,go:5,rust:5,cpp:5,php:5,tools:4,frontend:5,devops:5,mobile:5,dataeng:5,product:5,ux:5,architecture:5,backend:5,agent:13};
  var CLUSTER_AGENTS={triage:["triage"],correctness:["reviewer_correctness"],performance:["reviewer_performance","maint_perf","data_perf","sre_perf"],redundancy:["reviewer_redundancy","maint_debt"],security:["reviewer_security","security_engineer","sec_appsec","data_gov","sre_secops"],maintainability:["reviewer_maintainability","maint_refactor"],clarity:["reviewer_clarity","maint_clarity"],prompting:["reviewer_prompting","sec_prompt"],testing:["reviewer_testing","qa_engineer","maint_test","data_quality","sre_chaos"],api:["reviewer_api","sec_api"],observability:["reviewer_observability","sec_detection","sre_reliability"],documentation:["reviewer_documentation","tech_writer","maint_docs"],postgres:["specialist_postgresql","data_sql"],oracle:["specialist_oracle_plsql","data_oracle"],ruby:["specialist_ruby"],python:["specialist_python"],web:["specialist_web"],bash:["specialist_bash"],abap:["specialist_abap"],java:["specialist_java"],csharp:["specialist_csharp"],go:["specialist_go"],rust:["specialist_rust"],cpp:["specialist_cpp"],php:["specialist_php"],database:["specialist_database","sec_data","data_db"],erp:["specialist_erp"],ftz:["specialist_ftz"],wms:["specialist_wms"],frontend:["specialist_frontend","frontend_engineer"],devops:["specialist_devops","devops_engineer","sec_infra","sre_platform"],mobile:["specialist_mobile","mobile_engineer"],dataeng:["specialist_dataeng","data_engineer","data_pipeline"],product:["product_manager"],ux:["ux_designer"],architecture:["solution_architect","maint_arch"],backend:["backend_engineer","sre_backend"],overseer_eng:["overseer_mid_a","delivery_lead","sec_lead_blue","maint_lead","data_lead","sre_lead"],overseer_risk:["overseer_mid_b","quality_lead","sec_lead_red","maint_risk","data_risk","sre_risk"],synthesis:["overseer_chief","eng_director","sec_ciso","maint_chief","data_chief","sre_chief"]};
  var PACK_AGENT={postgresql:"specialist_postgresql",oracle_plsql:"specialist_oracle_plsql",ruby:"specialist_ruby",python:"specialist_python",typescript:"specialist_web",javascript:"specialist_web",bash:"specialist_bash",powershell:"specialist_bash",abap:"specialist_abap"};
  function agentFor(n,seatOf){ if(n.type==="agent")return null; if(n.type==="pack")return PACK_AGENT[n.id]||null; var cs=CLUSTER_AGENTS[n.type]; if(cs){for(var i=0;i<cs.length;i++){if(!seatOf||seatOf[cs[i]])return cs[i];}} return null; }
  function prettyAgent(id){ return id.replace(/^agent:/,"").replace(/^reviewer_/,"").replace(/^specialist_/,"").replace(/^overseer_/,"").replace(/_/g," "); }

  var nodes=[],links=[],byId={},agentNodes=[],brain=null;
  var raf=null,canvas=null,ctx=null,W=0,H=0,hover=null,inited=false,loadedData=null,wrap=null;
  var scale=1,panX=0,panY=0,fitted=false,focus=null;
  function dpr(){return window.devicePixelRatio||1;}

  function resize(){ canvas=document.getElementById("graphCanvas"); if(!canvas)return; wrap=canvas.parentElement; ctx=canvas.getContext("2d");
    var rect=canvas.getBoundingClientRect(); W=canvas.width=Math.max(1,Math.round(rect.width))*dpr(); H=canvas.height=Math.max(1,Math.round(rect.height))*dpr(); ctx.setTransform(dpr(),0,0,dpr(),0,0); }

  function fitView(){ var w=W/dpr(),h=H/dpr(),cx=w/2,cy=h/2,maxr=1;
    for(var i=0;i<nodes.length;i++){var n=nodes[i];var d=Math.hypot(n.x-cx,n.y-cy);if(d>maxr)maxr=d;}
    scale=Math.max(0.12,Math.min(1.3,(Math.min(w,h)/2-36)/(maxr+36))); panX=cx*(1-scale); panY=cy*(1-scale); }

  function layout(){ var w=W/dpr(),h=H/dpr(),cx=w/2,cy=h/2,m=Math.min(w,h);
    agentNodes=nodes.filter(function(n){return n.type==="agent";});
    var seatOf={}; agentNodes.forEach(function(a){seatOf[a.id.replace("agent:","")]=a;});
    var central=[],perAgent={};
    nodes.forEach(function(n){ if(n.type==="agent")return; var aid=agentFor(n,seatOf);
      if(aid&&seatOf[aid]){(perAgent[aid]=perAgent[aid]||[]).push(n);} else central.push(n); });
    agentNodes.forEach(function(a){a.label=prettyAgent(a.id);});

    // Brain core: sized to hold the unassigned ("central") notes as an even
    // golden-angle sunflower disk (no clumping, no type-wedge blob).
    var coreR=Math.max(m*0.11, 8.5*Math.sqrt(central.length||1));
    brain={x:cx,y:cy,r:coreR,count:nodes.filter(function(n){return n.type!=="agent";}).length};
    var GA=PI*(3-Math.sqrt(5));
    var nc=central.length||1;
    central.forEach(function(n,i){ var rr=coreR*0.92*Math.sqrt((i+0.5)/nc); var ang=i*GA;
      n.x=cx+Math.cos(ang)*rr; n.y=cy+Math.sin(ang)*rr; });

    // Agents on a ring, grouped: triage -> reviewers -> specialists -> others.
    var grp=function(a){var id=a.id.replace("agent:","");
      if(id.indexOf("triage")===0)return 0; if(id.indexOf("reviewer")===0)return 1; if(id.indexOf("specialist")===0)return 2; return 3;};
    var ring=agentNodes.slice().sort(function(a,b){return grp(a)-grp(b)||a.id.localeCompare(b.id);});
    var N=ring.length||1, sector=2*PI/N;
    var Ra=coreR+Math.max(m*0.13,96);
    ring.forEach(function(a,i){ var ang=-PI/2+i*sector; a.ang=ang; a.x=cx+Math.cos(ang)*Ra; a.y=cy+Math.sin(ang)*Ra; });

    // Each agent's knowledge fans OUTWARD within its own angular sector — rings
    // of increasing radius, each spread across the sector. Stays inside the
    // agent's slice so neighbouring clusters never collide.
    var aw=sector*0.80;            // angular width of each fan
    Object.keys(perAgent).forEach(function(aid){ var a=seatOf[aid]; if(!a)return; var list=perAgent[aid];
      var startR=Ra+30, rowGap=14, placed=0, j=0;
      while(placed<list.length){ var rr=startR+j*rowGap;
        var cap=Math.max(2, Math.round(aw*rr/13));      // arc-length capacity of this ring
        var nThis=Math.min(cap, list.length-placed);
        for(var c=0;c<nThis;c++){ var t=nThis<=1?0.5:c/(nThis-1); var ang=a.ang+(t-0.5)*aw;
          var node=list[placed+c]; node.x=cx+Math.cos(ang)*rr; node.y=cy+Math.sin(ang)*rr; }
        placed+=nThis; j++;
      }
    });
    if(!fitted){fitView();fitted=true;}
  }

  function renderStats(){ var c={}; nodes.forEach(function(n){c[n.type]=(c[n.type]||0)+1;});
    var notes=nodes.filter(function(n){return n.type!=="agent";}).length;
    function dot(col){return '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+col+';margin:0 3px 0 8px;vertical-align:middle"></span>';}
    var el=document.getElementById("graphStat"); if(!el)return;
    el.innerHTML=dot(COLORS.agent)+(c.agent||0)+" agents"+dot(COLORS.codegen)+(c.codegen||0)+" codegen"+dot(COLORS.book)+(c.book||0)+" library"+dot(COLORS.concept)+(c.concept||0)+" concepts"+dot(COLORS.security)+(c.security||0)+" security"+dot(COLORS.pack)+(c.pack||0)+" packs · "+notes+" notes"; }

  function themeVars(){
    var cs=getComputedStyle(document.documentElement);
    function v(name,fb){ return (cs.getPropertyValue(name)||"").trim()||fb; }
    return { fg:v("--fg","#e6edf3"), bg:v("--bg","#0f172a"), accent:v("--accent","#8c7cf8"),
             muted:v("--muted","#8d94a6"), border:v("--border","#2b3a52") };
  }

  function draw(){ var w=W/dpr(),h=H/dpr(); ctx.setTransform(dpr(),0,0,dpr(),0,0); ctx.clearRect(0,0,w,h);
    ctx.translate(panX,panY); ctx.scale(scale,scale); if(!brain)return;
    var TH=themeVars();
    // brain core — soft accent glow + themed ring
    ctx.beginPath(); ctx.arc(brain.x,brain.y,brain.r,0,2*PI);
    ctx.globalAlpha=0.06; ctx.fillStyle=TH.accent; ctx.fill(); ctx.globalAlpha=1;
    ctx.strokeStyle=TH.border; ctx.lineWidth=1; ctx.stroke();
    // note-note links (themed; visible enough to read, accent on hover)
    ctx.lineWidth=1;
    for(var i=0;i<links.length;i++){var l=links[i]; if(l.s.type==="agent"||l.t.type==="agent")continue;
      var on=(hover&&(l.s===hover||l.t===hover));
      ctx.globalAlpha=on?0.55:0.16; ctx.strokeStyle=on?TH.accent:TH.muted;
      ctx.beginPath(); ctx.moveTo(l.s.x,l.s.y); ctx.lineTo(l.t.x,l.t.y); ctx.stroke();}
    ctx.globalAlpha=1;
    // agent spokes to the core
    for(var a1=0;a1<agentNodes.length;a1++){var a=agentNodes[a1]; var ah=(a===hover);
      ctx.globalAlpha=ah?0.7:0.2; ctx.strokeStyle=ah?TH.accent:TH.border; ctx.lineWidth=ah?1.8:1.1;
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(brain.x,brain.y); ctx.stroke();}
    ctx.globalAlpha=1;
    // knowledge nodes — category colors kept (they encode the cluster)
    for(var k=0;k<nodes.length;k++){var n=nodes[k]; if(n.type==="agent")continue;
      var isF=focus&&n.type===focus; var r=(RAD[n.type]||4)*(isF?2.3:1);
      ctx.beginPath(); ctx.arc(n.x,n.y,r,0,2*PI); ctx.fillStyle=COLORS[n.type]||"#888";
      ctx.globalAlpha=focus?(isF?1:0.07):((hover&&hover!==n)?0.7:1);
      if(isF){ctx.shadowColor=COLORS[n.type]||"#fff";ctx.shadowBlur=20;}
      ctx.fill(); ctx.shadowBlur=0; ctx.globalAlpha=1;
      if(isF||n===hover){ctx.lineWidth=2;ctx.strokeStyle=TH.fg;ctx.stroke();}}
    // core labels
    ctx.fillStyle=TH.fg; ctx.textAlign="center"; ctx.font="600 13px system-ui,sans-serif";
    ctx.fillText("offline brain",brain.x,brain.y-brain.r-14);
    ctx.fillStyle=TH.muted; ctx.font="11px system-ui,sans-serif"; ctx.fillText(brain.count+" notes",brain.x,brain.y-brain.r);
    // agents — themed dots + quiet labels (bright on hover), shadowed for legibility
    for(var a2=0;a2<agentNodes.length;a2++){var ag=agentNodes[a2]; var agh=(ag===hover);
      ctx.beginPath(); ctx.arc(ag.x,ag.y,RAD.agent,0,2*PI); ctx.fillStyle=agh?TH.accent:TH.fg; ctx.fill();
      ctx.lineWidth=1.5; ctx.strokeStyle=agh?TH.accent:TH.border; ctx.stroke();
      ctx.save(); ctx.shadowColor=TH.bg; ctx.shadowBlur=6;
      ctx.fillStyle=agh?TH.fg:TH.muted; ctx.textAlign="center"; ctx.font=(agh?"600 12px":"500 11px")+" system-ui,sans-serif";
      var dy=ag.y<brain.y?-RAD.agent-6:RAD.agent+15; ctx.fillText(ag.label,ag.x,ag.y+dy); ctx.restore();}
  }
  function frame(){ if(canvas&&canvas.offsetParent===null){raf=requestAnimationFrame(frame);return;} draw();raf=requestAnimationFrame(frame);} function start(){if(raf)cancelAnimationFrame(raf);raf=requestAnimationFrame(frame);}

  function onMove(e){ if(!canvas||canvas.offsetParent===null)return;
    var rect=canvas.getBoundingClientRect(); var mx=e.clientX-rect.left,my=e.clientY-rect.top; hover=null; var bestD=Infinity;
    for(var i=0;i<nodes.length;i++){var n=nodes[i]; var sx=n.x*scale+panX,sy=n.y*scale+panY; var sr=(RAD[n.type]||4)*scale+7; var d=Math.hypot(sx-mx,sy-my); if(d<=sr&&d<bestD){bestD=d;hover=n;}}
    var tip=document.getElementById("graphTip"); if(!tip)return;
    if(hover){ tip.style.display="block"; tip.style.left=(mx+14)+"px"; tip.style.top=(my+14)+"px";
      if(hover.type==="agent"){var act=(hover.activation&&hover.activation[0]&&hover.activation[0]!=="always")?"activates on "+esc(hover.activation.join(", ")):"always on";
        tip.innerHTML="<b>"+esc(hover.label||prettyAgent(hover.id))+"</b><br><span style='color:#8aa0b6'>"+esc(hover.role||"agent")+" · "+act+"</span>";}
      else { tip.innerHTML="<b>"+esc(hover.title||hover.id)+"</b><br><span style='color:#8aa0b6'>"+esc(hover.type)+"</span>"; }
    } else tip.style.display="none";
  }
  function onWheel(e){ e.preventDefault(); var rect=canvas.getBoundingClientRect(); var mx=e.clientX-rect.left,my=e.clientY-rect.top;
    var wx=(mx-panX)/scale,wy=(my-panY)/scale; var factor=e.deltaY<0?1.12:1/1.12; scale=Math.max(0.15,Math.min(6,scale*factor)); panX=mx-wx*scale; panY=my-wy*scale; }
  function resetView(){focus=null;fitView();}
  function focusType(type){ focus=(focus===type)?null:type; if(!focus){fitView();return;}
    var pts=nodes.filter(function(n){return n.type===focus&&isFinite(n.x)&&isFinite(n.y);}); if(!pts.length){focus=null;fitView();return;}
    var minx=1e9,miny=1e9,maxx=-1e9,maxy=-1e9; for(var i=0;i<pts.length;i++){var n=pts[i];minx=Math.min(minx,n.x);maxx=Math.max(maxx,n.x);miny=Math.min(miny,n.y);maxy=Math.max(maxy,n.y);}
    var w=W/dpr(),h=H/dpr(); var cx=(minx+maxx)/2,cy=(miny+maxy)/2;
    var spanx=Math.max(1,maxx-minx),spany=Math.max(1,maxy-miny);
    scale=Math.max(0.2,Math.min(4,Math.min((w-80)/spanx,(h-80)/spany)));
    panX=w/2-cx*scale; panY=h/2-cy*scale;
  }
  // ---- data load + show (council 2D galaxy; also window.__graphShow2D fallback for the 3D view) ----
  function load(){ var d=window.__BG; if(!d||!d.nodes)return false;
    nodes=d.nodes; links=[]; byId={}; nodes.forEach(function(n){byId[n.id]=n;});
    (d.links||[]).forEach(function(l){var s=byId[l.source],t=byId[l.target]; if(s&&t)links.push({s:s,t:t});});
    loadedData=d;
    return true; }
  function show(){ resize(); if(!canvas)return; var changed=loadedData!==window.__BG;
    if(!inited){ if(!load())return; inited=true;
      canvas.addEventListener("mousemove",onMove);
      canvas.addEventListener("wheel",onWheel,{passive:false});
      canvas.addEventListener("dblclick",resetView);
      window.addEventListener("resize",function(){resize();layout();});
    } else if(changed){ if(!load())return; fitted=false; focus=null; hover=null; }
    renderStats(); layout();
    start(); }
  window.__graphShow2D=show;
  window.__graphFocusType=focusType;
  window.__graphResetView=resetView;
})();
