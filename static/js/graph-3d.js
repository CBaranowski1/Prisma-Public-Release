/* 3D "brain galaxy" with a central black hole — self-contained WebGL (no Three.js,
   no CDN, offline). A spiral galaxy of ~6,500 knowledge stars on four arms, with a
   physically-motivated black hole at the core: dark event-horizon shadow, bright
   photon ring, a flat accretion disk with a blackbody temperature gradient (blue-white
   inner -> orange -> red outer) and Doppler beaming, a perpendicular lensed ring, and
   the brain-core knowledge spiralling in as infalling matter. Free-fly camera: mouse
   looks, WASD/QE fly with momentum. Falls back to window.__graphShow2D on any failure. */
"use strict";
(function () {
  var COLORS = { concept:"#9b7cff", pack:"#5b9dff", ftz:"#46c7c7", book:"#e07b53",
    trade:"#d65db1", recipe:"#5bd1a0", security:"#ee5a6f", maintainability:"#5ec2e0",
    clarity:"#9fe870", prompting:"#c77dff", triage:"#ffa94d", correctness:"#ff5252",
    performance:"#4dabf7", redundancy:"#b8a89a", synthesis:"#f2cd60", overseer_eng:"#7ee0c0",
    overseer_risk:"#ffb27a", web:"#61dafb", bash:"#89e051", abap:"#0a6ed1", oracle:"#c74634",
    ruby:"#cc342d", postgres:"#6aa3e0", python:"#ffd43b", codegen:"#ff7eb6", access:"#d2615f",
    domain:"#46c7c7", imported:"#94a7bd", conversation:"#41c980", lesson:"#e0a93b",
    review:"#8a97ad", database:"#7aa2f7", erp:"#1fd0b6", wms:"#f4a261",
    java:"#e76f00", csharp:"#9b4f96", go:"#00add8", rust:"#dea584", cpp:"#00599c", php:"#777bb4",
    testing:"#63e6be", api:"#74c0fc", observability:"#ffd43b", documentation:"#b0c4de", agentsdk:"#da77f2",
    tools:"#ffd166", frontend:"#ff6b9d", devops:"#5eead4", mobile:"#a78bfa", dataeng:"#fb923c",
    product:"#f0abfc", ux:"#67e8f9", architecture:"#fcd34d", backend:"#86efac", ml:"#ff5d8f",
    accessibility:"#36c5f0", depsec:"#ff8c42", concurrency:"#b388ff", meta:"#ffd700",
    privacy:"#e84393", resilience:"#00b894", migration:"#fab1a0", iac:"#6c5ce7",
    finops:"#00cec9", i18n:"#fd79a8", licensing:"#dfe6e9", distributed:"#a29bfe",
    ceo:"#ffd93b", release:"#34d399", devex:"#22d3ee", retro:"#c084fc",
    reflection:"#ffe27a", brain:"#eaf6ff",
    agent:"#ffffff" };
  function rgb(h){ var n=parseInt(h.slice(1),16); return [((n>>16)&255)/255,((n>>8)&255)/255,(n&255)/255]; }
  function lerp3(a,b,t){ return [a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, a[2]+(b[2]-a[2])*t]; }
  var NODE_AGENT={ postgres:"specialist_postgresql", ruby:"specialist_ruby", python:"specialist_python",
    security:"reviewer_security", maintainability:"reviewer_maintainability", clarity:"reviewer_clarity",
    prompting:"reviewer_prompting", triage:"triage", correctness:"reviewer_correctness",
    performance:"reviewer_performance", redundancy:"reviewer_redundancy", synthesis:"overseer_chief",
    overseer_eng:"overseer_mid_a", overseer_risk:"overseer_mid_b", web:"specialist_web",
    bash:"specialist_bash", abap:"specialist_abap", oracle:"specialist_oracle_plsql",
    ftz:"specialist_ftz", database:"specialist_database", erp:"specialist_erp", wms:"specialist_wms",
    java:"specialist_java", csharp:"specialist_csharp", go:"specialist_go", rust:"specialist_rust",
    cpp:"specialist_cpp", php:"specialist_php", frontend:"specialist_frontend", devops:"specialist_devops",
    mobile:"specialist_mobile", dataeng:"specialist_dataeng", testing:"reviewer_testing",
    api:"reviewer_api", observability:"reviewer_observability", documentation:"reviewer_documentation",
    architecture:"specialist_architecture", agentsdk:"specialist_agentsdk", accessibility:"reviewer_accessibility",
    depsec:"reviewer_depsec", concurrency:"reviewer_concurrency", meta:"specialist_meta",
    backend:"specialist_backend", ml:"specialist_ml", ux:"reviewer_ux", product:"reviewer_product",
    privacy:"reviewer_privacy", resilience:"reviewer_resilience", migration:"reviewer_migration",
    iac:"specialist_iac", finops:"reviewer_finops", i18n:"reviewer_i18n", licensing:"reviewer_licensing",
    distributed:"specialist_distributed", codegen:"specialist_codegen", domain:"specialist_domain",
    access:"specialist_access", trade:"specialist_trade",
    ceo:"specialist_ceo", release:"specialist_release", devex:"specialist_devex", retro:"specialist_retro" };
  var PACK_AGENT={ postgresql:"specialist_postgresql", oracle_plsql:"specialist_oracle_plsql",
    ruby:"specialist_ruby", python:"specialist_python", typescript:"specialist_web",
    javascript:"specialist_web", bash:"specialist_bash", powershell:"specialist_bash", abap:"specialist_abap" };
  function agentFor(n){ if(n.type==="agent")return null; if(n.type==="pack")return PACK_AGENT[n.id]||null; return NODE_AGENT[n.type]||null; }
  function pretty(id){ return id.replace(/^agent:/,"").replace(/^reviewer_/,"").replace(/^specialist_/,"").replace(/^overseer_/,"").replace(/_/g," "); }

  function mPersp(f,a,n,fr){ var t=1/Math.tan(f/2),d=1/(n-fr); return new Float32Array([t/a,0,0,0, 0,t,0,0, 0,0,(fr+n)*d,-1, 0,0,(2*fr*n)*d,0]); }
  function mMul(a,b){ var o=new Float32Array(16),c,r; for(c=0;c<4;c++)for(r=0;r<4;r++)o[c*4+r]=a[r]*b[c*4]+a[4+r]*b[c*4+1]+a[8+r]*b[c*4+2]+a[12+r]*b[c*4+3]; return o; }
  function mRotY(a){ var c=Math.cos(a),s=Math.sin(a); return new Float32Array([c,0,-s,0, 0,1,0,0, s,0,c,0, 0,0,0,1]); }
  function mApply(m,x,y,z){ return [m[0]*x+m[4]*y+m[8]*z+m[12], m[1]*x+m[5]*y+m[9]*z+m[13], m[2]*x+m[6]*y+m[10]*z+m[14], m[3]*x+m[7]*y+m[11]*z+m[15]]; }
  function vCross(a,b){ return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]; }
  function vDot(a,b){ return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
  function vNorm(a){ var l=Math.hypot(a[0],a[1],a[2])||1; return [a[0]/l,a[1]/l,a[2]/l]; }
  function mLookAt(e,f,up){ var s=vNorm(vCross(f,up)), u=vCross(s,f);
    return new Float32Array([ s[0],u[0],-f[0],0, s[1],u[1],-f[1],0, s[2],u[2],-f[2],0, -vDot(s,e),-vDot(u,e),vDot(f,e),1 ]); }

  var PVS="attribute vec3 aPos;attribute vec3 aCol;attribute float aSize;attribute float aPhase;"+
    "uniform mat4 uMVP;uniform float uK;uniform float uTime;uniform float uFogN;uniform float uFogF;"+
    "varying vec3 vCol;varying float vFog;void main(){vec4 p=uMVP*vec4(aPos,1.0);gl_Position=p;"+
    "float tw=0.84+0.16*sin(uTime*1.7+aPhase);gl_PointSize=clamp(aSize*tw*uK/max(p.w,0.001),1.0,160.0);"+
    "vCol=aCol;vFog=clamp((p.w-uFogN)/max(1.0,uFogF-uFogN),0.0,1.0);}";
  var PFS="precision mediump float;varying vec3 vCol;varying float vFog;void main(){"+
    "vec2 c=gl_PointCoord-0.5;float d=length(c);float glow=smoothstep(0.5,0.0,d);if(glow<=0.0)discard;"+
    "float hot=smoothstep(0.17,0.0,d);vec3 col=mix(vCol,vec3(1.0),hot*0.45);"+
    "float lum=dot(col,vec3(0.299,0.587,0.114));col=clamp(mix(vec3(lum),col,1.38),0.0,1.0);"+
    "float a=glow*(1.0-vFog*0.82);gl_FragColor=vec4(col,a);}";
  var LVS="attribute vec3 aPos;attribute vec3 aCol;uniform mat4 uMVP;uniform float uFogN;uniform float uFogF;"+
    "varying vec3 vCol;varying float vFog;void main(){vec4 p=uMVP*vec4(aPos,1.0);gl_Position=p;vCol=aCol;"+
    "vFog=clamp((p.w-uFogN)/max(1.0,uFogF-uFogN),0.0,1.0);}";
  var LFS="precision mediump float;varying vec3 vCol;varying float vFog;void main(){gl_FragColor=vec4(vCol,0.18*(1.0-vFog*0.85));}";
  var SVS="attribute vec3 aPos;attribute float aSize;uniform mat4 uMVP;uniform float uK;void main(){"+
    "vec4 p=uMVP*vec4(aPos,1.0);gl_Position=p;gl_PointSize=clamp(aSize*uK/max(p.w,0.001),2.0,2200.0);}";
  var SFS="precision mediump float;void main(){vec2 c=gl_PointCoord-0.5;float d=length(c);"+
    "float a=smoothstep(0.5,0.30,d);gl_FragColor=vec4(0.012,0.015,0.025,a);}";

  var wrap, canvas, gl, pProg, lProg, shProg, raf=0, inited=false, loadedData=null, labelBox, hud, tip, t0=0;
  var posBuf,colBuf,sizBuf,phaBuf,lPosBuf,lColBuf,nPoints=0,nLine=0;
  var bPosBuf,bColBuf,bSizBuf,bPhaBuf,nBH=0, shPosBuf,shSizBuf;
  var pulsePosBuf,pulseColBuf,pulseSizBuf,pulsePhaBuf,pulseXYZ,AP=[],nPulse=0;
  var P=[], drawnIdx=[], nodes=[], agentNodes=[], agentIdx=[], DPR=1, RHOR=95, lastMVP=null, _pickT=0, _lastFrame=0;
  var eye=[0,760,2650], yaw=0, pitch=-0.28, vel=[0,0,0], keys={}, spin=0, spinOn=true, look=false, lx=0, ly=0;
  var SX=[], SY=[], SV=[], hover=-1, mX=-1, mY=-1, _pmx=-1, _pmy=-1, seed=2466;
  var MEM={}, downX=0, downY=0, moved=false;
  function rnd(){ seed=(seed*1664525+1013904223)&0x7fffffff; return seed/0x7fffffff; }
  function inBall(R){ var u=rnd()*2-1, t=rnd()*6.2832, rr=Math.cbrt(rnd())*R, s=Math.sqrt(1-u*u); return [rr*s*Math.cos(t), rr*s*Math.sin(t), rr*u]; }

  function mkProg(vs,fs){ var p=gl.createProgram(),a=gl.createShader(gl.VERTEX_SHADER),b=gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(a,vs);gl.compileShader(a);gl.shaderSource(b,fs);gl.compileShader(b);
    gl.attachShader(p,a);gl.attachShader(p,b);gl.linkProgram(p);
    if(!gl.getProgramParameter(p,gl.LINK_STATUS)) throw new Error("link "+gl.getShaderInfoLog(a)+gl.getShaderInfoLog(b)); return p; }
  function buf(arr,dyn){ var b=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,b); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(arr),dyn?gl.DYNAMIC_DRAW:gl.STATIC_DRAW); return b; }
  function attr(prog,name,b,size){ var l=gl.getAttribLocation(prog,name); if(l<0)return; gl.bindBuffer(gl.ARRAY_BUFFER,b); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l,size,gl.FLOAT,false,0,0); }
  function uni(prog,name,v){ var l=gl.getUniformLocation(prog,name); if(l)gl.uniform1f(l,v); }

  function tempColor(t){ var hot=[0.78,0.88,1.0], mid=[1.0,0.82,0.5], cool=[0.86,0.28,0.12];
    return t<0.5 ? lerp3(hot,mid,t/0.5) : lerp3(mid,cool,(t-0.5)/0.5); }

  function buildScene(){
    seed=2466;
    var D=window.__BG||{nodes:[],links:[]}; nodes=D.nodes||[];
    var idx={}; nodes.forEach(function(n,i){ idx[n.id]=i; });
    agentNodes = nodes.filter(function(n){ return n.type==="agent"; });
    // Indices of the agent nodes — labels project only these every frame (cheap),
    // instead of looping all (potentially 76k+) nodes each frame.
    agentIdx = agentNodes.map(function(a){ return idx[a.id]; }).filter(function(i){ return i != null; });
    var ARMS=4, R_IN=470, R_OUT=1860, TWIST=0.0056;
    function disk(r,th,y){ return [r*Math.cos(th), y, r*Math.sin(th)]; }
    var grp=function(a){ var id=a.id.replace("agent:",""); return id.indexOf("triage")===0?0:id.indexOf("reviewer")===0?1:id.indexOf("specialist")===0?2:3; };
    var ring=agentNodes.slice().sort(function(a,b){ return grp(a)-grp(b)||a.id.localeCompare(b.id); });
    var apos={}, A=ring.length;
    ring.forEach(function(a,i){ var u=A>1?i/(A-1):0.5, r=R_IN+u*(R_OUT-R_IN), arm=i%ARMS, th=arm*(6.2832/ARMS)+r*TWIST;
      apos[a.id.replace("agent:","")]={r:r,th:th}; });
    var per={}, central=[], refl=[], REFLP=[];
    nodes.forEach(function(n){ if(n.type==="agent")return;
      if(n.type==="reflection"){ refl.push(n); return; }
      var aid=agentFor(n); if(aid&&apos[aid]){(per[aid]=per[aid]||[]).push(n);} else central.push(n); });

    var pos=[],col=[],siz=[],pha=[], bpos=[],bcol=[],bsiz=[],bpha=[]; P=new Array(nodes.length);
    function add(x,y,z,c,s){ pos.push(x,y,z); col.push(c[0],c[1],c[2]); siz.push(s); pha.push(rnd()*6.2832); }
    function place(i,x,y,z,c,s){ P[i]=[x,y,z]; add(x,y,z,c,s); }
    function addB(x,y,z,c,s){ bpos.push(x,y,z); bcol.push(c[0],c[1],c[2]); bsiz.push(s); bpha.push(rnd()*6.2832); }
    function placeB(i,x,y,z,c,s){ P[i]=[x,y,z]; addB(x,y,z,c,s); }

    // agents along arms + their knowledge smeared down each arm (galaxy = main buffer)
    ring.forEach(function(a){ var ap=apos[a.id.replace("agent:","")], p=disk(ap.r,ap.th,18); place(idx[a.id],p[0],p[1],p[2],[1,1,1],60); });
    // LOD cap: very large clusters (rust, c++, postgres, oracle, wms, the 1k+
    // language clusters) draw a dense representative sample instead of every star,
    // so the galaxy stays smooth. All notes remain in the vault / agent memory.
    var DRAW_CAP=900;
    Object.keys(per).forEach(function(aid){ var ap=apos[aid], list=per[aid];
      if(list.length>DRAW_CAP){ var st=list.length/DRAW_CAP, samp=[];
        for(var si=0; si<list.length; si+=st) samp.push(list[si|0]); list=samp; }
      var spreadR=Math.min(60+Math.sqrt(list.length)*7,340), thick=34+46*(1-ap.r/R_OUT);
      list.forEach(function(n){ var dr=(rnd()-0.5)*2*spreadR, dth=(rnd()-0.5)*(spreadR/ap.r),
        r=Math.max(R_IN*0.9,ap.r+dr), th=ap.th+dth+dr*TWIST, y=(rnd()-0.5+rnd()-0.5)*thick,
        p=disk(r,th,y), c=rgb(COLORS[n.type]||"#8aa0b6"); place(idx[n.id],p[0],p[1],p[2],c,12); }); });
    ring.forEach(function(a){ var ap=apos[a.id.replace("agent:","")], p=disk(ap.r,ap.th,18); add(p[0],p[1],p[2],[0.34,0.46,0.62],128); });
    // deep-space background grounded in real imagery: stars graded by stellar
    // temperature (blue->white->yellow->orange), emission/reflection nebulae
    // (H-alpha crimson/pink, O III teal, reflection blue), faint far galaxies.
    var SCOL=[[0.95,0.97,1.0],[0.95,0.97,1.0],[0.95,0.97,1.0],[0.78,0.85,1.0],[0.78,0.85,1.0],
              [0.62,0.74,1.0],[1.0,0.95,0.86],[1.0,0.82,0.6]];
    for(var k=0;k<2000;k++){ var o=inBall(1), R2=2300+rnd()*3000, L=Math.hypot(o[0],o[1],o[2])||1, sc=SCOL[(rnd()*8)|0];
      add(o[0]/L*R2,o[1]/L*R2,o[2]/L*R2,sc,1.3+rnd()*2.6); }
    // SELF-LEARNING REFLECTIONS — bright nodes in a ring just outside the core,
    // one per conversation. Spokes to every agent are added in the links section.
    var _rc=rgb(COLORS.reflection||"#ffe27a");
    refl.forEach(function(n,i){ var a=(i/Math.max(1,refl.length))*6.2832, rr=R_IN*0.6, y=(rnd()-0.5)*26,
      p=disk(rr,a,y); place(idx[n.id], p[0],p[1],p[2], _rc, 22); REFLP.push(p); });
    nPoints=pos.length/3; posBuf=buf(pos); colBuf=buf(col); sizBuf=buf(siz); phaBuf=buf(pha);
    // Only nodes with a real 3D position are drawn/hoverable. Cache their indices
    // so the per-frame projection + hover-pick scan that list instead of all
    // (76k+) nodes, the vast majority of which are never placed on screen.
    drawnIdx=[]; for(var _di=0; _di<P.length; _di++){ if(P[_di]) drawnIdx.push(_di); }

    // --- ACTUAL BLACK HOLE: a dark event-horizon sphere wrapped by a bright photon
    // ring and a Doppler-beamed, gravitationally-lensed accretion disk built FROM
    // the knowledge nodes themselves (the "central" notes ARE the horizon matter). ---
    var rIn=RHOR*1.5, rOut=RHOR*4.4;
    // the nodes = infalling matter forming the accretion disk at the event horizon
    central.forEach(function(n){ var ang=rnd()*6.2832, t=Math.pow(rnd(),0.6), rr=rIn+t*(rOut-rIn),
      beam=0.42+1.06*(0.5+0.5*Math.cos(ang)), y=(rnd()-0.5)*7*(rr/rOut),
      c0=rgb(COLORS[n.type]||"#ffb070"), c=[Math.min(1,c0[0]*beam),Math.min(1,c0[1]*beam),Math.min(1,c0[2]*beam)];
      placeB(idx[n.id], rr*Math.cos(ang), y, rr*Math.sin(ang), c, 11); });
    // synthetic hot plasma filling the disk between nodes (blackbody + Doppler beaming)
    for(var j=0;j<1600;j++){ var ang=rnd()*6.2832, t=Math.pow(rnd(),0.7), rr=rIn+t*(rOut-rIn),
      c=tempColor(t), b=0.24+0.62*(0.5+0.5*Math.cos(ang)), y=(rnd()-0.5)*7*(rr/rOut);
      addB(rr*Math.cos(ang), y, rr*Math.sin(ang), [Math.min(1,c[0]*b),Math.min(1,c[1]*b),Math.min(1,c[2]*b)], 6); }
    // PHOTON RING — bright thin Einstein ring hugging the shadow (the signature glow)
    for(var pr=0;pr<420;pr++){ var a2=pr/420*6.2832, rr2=RHOR*1.28+(rnd()-0.5)*2.2;
      addB(rr2*Math.cos(a2),(rnd()-0.5)*1.6,rr2*Math.sin(a2),[0.62,0.58,0.50],5); }
    // LENSED RING — the disk's far side bent up and over the hole (vertical ring)
    for(var lr=0;lr<340;lr++){ var a3=lr/340*6.2832, rr3=RHOR*1.34+(rnd()-0.5)*3;
      addB(rr3*Math.cos(a3),rr3*Math.sin(a3),(rnd()-0.5)*1.6,[0.60,0.50,0.34],4); }
    nBH=bpos.length/3; bPosBuf=buf(bpos); bColBuf=buf(bcol); bSizBuf=buf(bsiz); bPhaBuf=buf(bpha);
    // event-horizon shadow — the dark sphere the disk and photon ring wrap around
    shPosBuf=buf([0,0,0]); shSizBuf=buf([RHOR*2.4]);

    // links (sampled) + agent->core infall spokes
    var lp=[],lc=[],lk=D.links||[],cap=9000,step=Math.max(1,Math.floor(lk.length/cap));
    for(var li=0; li<lk.length; li+=step){ var s=idx[lk[li].source], t2=idx[lk[li].target];
      if(s==null||t2==null||!P[s]||!P[t2])continue; if(nodes[s].type==="agent"||nodes[t2].type==="agent")continue;
      lp.push(P[s][0],P[s][1],P[s][2],P[t2][0],P[t2][1],P[t2][2]); lc.push(0.4,0.5,0.66,0.4,0.5,0.66); }
    AP=[]; agentNodes.forEach(function(a){ var p=P[idx[a.id]]; if(!p)return; AP.push(p);
      lp.push(p[0],p[1],p[2],0,0,0); lc.push(0.32,0.78,0.86,0.95,0.6,0.35); });
    // SELF-LEARNING WEB — bright spoke from every reflection to every agent
    var _rl=rgb(COLORS.reflection||"#ffe27a"), _rcap=REFLP.slice(-40);
    for(var rri=0; rri<_rcap.length; rri++){ var _rp=_rcap[rri];
      agentNodes.forEach(function(a){ var ap=P[idx[a.id]]; if(!ap)return;
        lp.push(_rp[0],_rp[1],_rp[2], ap[0],ap[1],ap[2]);
        lc.push(_rl[0],_rl[1],_rl[2], _rl[0],_rl[1],_rl[2]); }); }
    nLine=lp.length/3; lPosBuf=buf(lp); lColBuf=buf(lc);
    nPulse=AP.length; pulseXYZ=new Float32Array(nPulse*3);
    var pc=[],ps=[],pp=[]; for(var q=0;q<nPulse;q++){ pc.push(1.0,0.8,0.45); ps.push(17); pp.push(0); }
    pulsePosBuf=buf(pulseXYZ,true); pulseColBuf=buf(pc); pulseSizBuf=buf(ps); pulsePhaBuf=buf(pp);
    var st=document.getElementById("graphStat"); if(st){ var total=(D.stats&&D.stats.nodes)||nodes.length;
      st.textContent=nodes.length+" visible / "+total+" indexed nodes · "+agentNodes.length+" agents · 1 black hole"; }
    MEM=per;
    var agEl=document.getElementById("graph3dAgents");
    if(agEl){
      var order=["Triage","Engineering","Risk & Hygiene","Overseers"];
      var byTeam={};
      ring.forEach(function(a){ var g=a.group||"Other"; (byTeam[g]=byTeam[g]||[]).push({id:a.id, n:(per[a.id.replace("agent:","")]||[]).length}); });
      Object.keys(byTeam).forEach(function(g){ byTeam[g].sort(function(x,y){ return y.n-x.n; }); });
      var teams=order.filter(function(g){ return byTeam[g]; }).concat(Object.keys(byTeam).filter(function(g){ return order.indexOf(g)<0; }));
      var h="<div style='color:var(--fg, #cfeaf2);font-weight:600;letter-spacing:1.2px;margin-bottom:5px'>Legend</div>";
      teams.forEach(function(g){ var rows=byTeam[g];
        h+="<div style='color:var(--accent, #7fe0d6);letter-spacing:1px;margin:7px 0 2px;border-bottom:1px solid var(--border, rgba(70,210,225,.18));padding-bottom:2px;font-size:9.5px'>"+esc(g.toUpperCase())+" <span style='color:var(--muted, #6b7699);float:right'>"+rows.length+"</span></div>";
        rows.forEach(function(r){ h+="<div onclick=\"window.__g3dAgent('"+r.id+"')\" style='display:flex;justify-content:space-between;gap:8px;cursor:pointer;padding:1px 3px;border-radius:4px' onmouseover=\"this.style.background='color-mix(in srgb, var(--accent) 14%, transparent)'\" onmouseout=\"this.style.background='none'\"><span style='color:var(--fg, #bcd3df);white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"+esc(pretty(r.id))+"</span><span style='color:var(--accent, #6fe0d0)'>"+r.n+"</span></div>"; });
      });
      agEl.innerHTML=h; }
  }

  function ensureDom(){
    wrap=document.getElementById("graphWrap");
    var c2=document.getElementById("graphCanvas"); if(c2) c2.style.display="none";
    canvas=document.getElementById("graph3dCanvas");
    if(!canvas){ canvas=document.createElement("canvas"); canvas.id="graph3dCanvas";
      canvas.style.cssText="position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab;z-index:1"; wrap.appendChild(canvas); }
    hud=document.getElementById("graph3dHud");
    if(!hud){ hud=document.createElement("div"); hud.id="graph3dHud";
      hud.style.cssText="position:absolute;inset:0;z-index:2;pointer-events:none;overflow:hidden";
      hud.innerHTML =
        "<div style='position:absolute;inset:0;background:radial-gradient(125% 120% at 50% 44%,rgba(0,0,0,0) 56%,color-mix(in srgb, var(--bg) 55%, transparent) 100%)'></div>";
      wrap.appendChild(hud); }
    if(!document.getElementById("graph3dControls")){
      var ctl=document.createElement("div"); ctl.id="graph3dControls"; ctl.className="bg-controls";
      ctl.innerHTML="Controls"+
        "<div class='bg-controls-keys'>"+
        "<div><b>mouse</b> look</div><div><b>W A S D</b> fly</div>"+
        "<div><b>Q / E</b> down / up</div><div><b>shift</b> boost</div>"+
        "<div><b>scroll</b> fwd / back</div><div><b>R</b> reset &nbsp; <b>space</b> spin</div>"+
        "</div>";
      wrap.appendChild(ctl);
    }
    var agp=document.getElementById("graph3dAgents");
    if(!agp){ agp=document.createElement("div"); agp.id="graph3dAgents";
      agp.className="bg-legend"; agp.setAttribute("hidden","");
      wrap.appendChild(agp); }
    var dtl=document.getElementById("graph3dDetail");
    if(!dtl){ dtl=document.createElement("div"); dtl.id="graph3dDetail";
      dtl.style.cssText="position:absolute;right:12px;top:46px;width:332px;max-height:calc(100% - 70px);overflow-y:auto;z-index:5;pointer-events:auto;display:none;background:color-mix(in srgb, var(--bg) 90%, transparent);border:1px solid var(--border, rgba(70,210,225,.32));border-radius:9px;padding:11px 13px;font:12px/1.6 system-ui,sans-serif;color:var(--fg, #cfe1ee);box-shadow:var(--shadow, 0 8px 30px rgba(0,0,0,.5))";
      wrap.appendChild(dtl); }
    labelBox=document.getElementById("graph3dLabels");
    if(!labelBox){ labelBox=document.createElement("div"); labelBox.id="graph3dLabels";
      labelBox.style.cssText="position:absolute;inset:0;z-index:3;pointer-events:none;font:700 12.5px system-ui,sans-serif"; wrap.appendChild(labelBox); }
    tip=document.getElementById("graphTip");
  }
  // Cap render resolution: on a high-DPI display devicePixelRatio is 2 (or more),
  // which quadruples the fragments the glow shader paints for every star. 1.5 is
  // visually indistinguishable here but a big GPU saving on the dense galaxy.
  function resize(){ DPR=Math.min(window.devicePixelRatio||1, 1.5); var r=wrap.getBoundingClientRect();
    canvas.width=Math.max(1,Math.round(r.width*DPR)); canvas.height=Math.max(1,Math.round(r.height*DPR)); gl.viewport(0,0,canvas.width,canvas.height); }

  function frame(){
    raf=requestAnimationFrame(frame);
    // When the 2D map is showing, this 3D canvas is hidden — idle the loop so
    // it doesn't render or clobber the shared #graphTip (which killed 2D hover).
    if(canvas&&canvas.offsetParent===null) return;
    // Render at ~30fps unless actively flying/dragging (then full rate for a
    // smooth gesture). Halves GPU load on the dense galaxy when just spinning.
    var nowMs=performance.now();
    var interacting = look || keys["w"]||keys["a"]||keys["s"]||keys["d"]||keys["q"]||keys["e"]
      || keys["shift"]||keys["arrowup"]||keys["arrowdown"]||keys["arrowleft"]||keys["arrowright"];
    if(!interacting && nowMs-_lastFrame < 32) return;
    _lastFrame=nowMs;
    var tm=(performance.now()/1000)-t0;
    var cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch);
    var fwd=[cp*sy, sp, -cp*cy], rgt=vNorm(vCross(fwd,[0,1,0]));
    var ax=0,ay=0,az=0;
    if(keys["w"]||keys["arrowup"]){ ax+=fwd[0]; ay+=fwd[1]; az+=fwd[2]; }
    if(keys["s"]||keys["arrowdown"]){ ax-=fwd[0]; ay-=fwd[1]; az-=fwd[2]; }
    if(keys["d"]||keys["arrowright"]){ ax+=rgt[0]; ay+=rgt[1]; az+=rgt[2]; }
    if(keys["a"]||keys["arrowleft"]){ ax-=rgt[0]; ay-=rgt[1]; az-=rgt[2]; }
    if(keys["e"]) ay+=1; if(keys["q"]) ay-=1;
    var boost=keys["shift"]?3.0:1.0, accel=2.6*boost, L=Math.hypot(ax,ay,az);
    if(L>0){ vel[0]+=ax/L*accel; vel[1]+=ay/L*accel; vel[2]+=az/L*accel; }
    vel[0]*=0.86; vel[1]*=0.86; vel[2]*=0.86; eye[0]+=vel[0]; eye[1]+=vel[1]; eye[2]+=vel[2];
    var el=Math.hypot(eye[0],eye[1],eye[2]); if(el>7200){ var fc=7200/el; eye[0]*=fc; eye[1]*=fc; eye[2]*=fc; }
    if(spinOn) spin+=0.0009;
    var V=mLookAt(eye,fwd,[0,1,0]), M=mRotY(spin);
    var Pr=mPersp(0.9, canvas.width/canvas.height, 1, 11000);
    var MVP=mMul(Pr, mMul(V, M)), fogN=360, fogF=7000;
    gl.clearColor(0.012,0.016,0.03,1); gl.clear(gl.COLOR_BUFFER_BIT);
    // galaxy links
    gl.useProgram(lProg); gl.uniformMatrix4fv(gl.getUniformLocation(lProg,"uMVP"),false,MVP); uni(lProg,"uFogN",fogN); uni(lProg,"uFogF",fogF);
    attr(lProg,"aPos",lPosBuf,3); attr(lProg,"aCol",lColBuf,3); gl.drawArrays(gl.LINES,0,nLine);
    // galaxy stars (additive)
    gl.useProgram(pProg); gl.uniformMatrix4fv(gl.getUniformLocation(pProg,"uMVP"),false,MVP);
    uni(pProg,"uK",canvas.height); uni(pProg,"uTime",tm); uni(pProg,"uFogN",fogN); uni(pProg,"uFogF",fogF);
    attr(pProg,"aPos",posBuf,3); attr(pProg,"aCol",colBuf,3); attr(pProg,"aSize",sizBuf,1); attr(pProg,"aPhase",phaBuf,1);
    gl.drawArrays(gl.POINTS,0,nPoints);
    // event-horizon shadow (alpha blend, darkens what is behind)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(shProg); gl.uniformMatrix4fv(gl.getUniformLocation(shProg,"uMVP"),false,MVP); uni(shProg,"uK",canvas.height);
    attr(shProg,"aPos",shPosBuf,3); attr(shProg,"aSize",shSizBuf,1); gl.drawArrays(gl.POINTS,0,1);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
    // accretion disk + rings (additive, over the shadow)
    gl.useProgram(pProg); gl.uniformMatrix4fv(gl.getUniformLocation(pProg,"uMVP"),false,MVP);
    attr(pProg,"aPos",bPosBuf,3); attr(pProg,"aCol",bColBuf,3); attr(pProg,"aSize",bSizBuf,1); attr(pProg,"aPhase",bPhaBuf,1);
    gl.drawArrays(gl.POINTS,0,nBH);
    // infall pulses
    if(nPulse){ for(var i=0;i<nPulse;i++){ var f=1-(((tm*0.3)+(i*0.16))%1); pulseXYZ[i*3]=AP[i][0]*f; pulseXYZ[i*3+1]=AP[i][1]*f; pulseXYZ[i*3+2]=AP[i][2]*f; }
      gl.bindBuffer(gl.ARRAY_BUFFER,pulsePosBuf); gl.bufferData(gl.ARRAY_BUFFER,pulseXYZ,gl.DYNAMIC_DRAW);
      attr(pProg,"aPos",pulsePosBuf,3); attr(pProg,"aCol",pulseColBuf,3); attr(pProg,"aSize",pulseSizBuf,1); attr(pProg,"aPhase",pulsePhaBuf,1);
      gl.drawArrays(gl.POINTS,0,nPulse); }
    lastMVP=MVP;
    // Labels (agents only) update every frame — cheap. The FULL 76k projection
    // and hover-pick are the expensive loops, so run them at most ~15x/sec and
    // ONLY while the cursor is over the galaxy (idle/spinning pays nothing). This
    // is what kept big graphs from freezing on click / stop-spin.
    projectAgents(MVP); placeLabels();
    if(mX>=0 && !look){ if((mX!==_pmx||mY!==_pmy) && performance.now()-_pickT>=66){ _pickT=performance.now(); _pmx=mX; _pmy=mY; project(MVP); pick(); } }
    else if(tip){ tip.style.display="none"; hover=-1; }
  }
  function project(MVP){ var W=canvas.width/DPR, H=canvas.height/DPR;
    for(var di=0;di<drawnIdx.length;di++){ var i=drawnIdx[di], p=P[i]; if(!p){SV[i]=false;continue;} var c=mApply(MVP,p[0],p[1],p[2]);
      if(c[3]<=0){SV[i]=false;continue;} SX[i]=(c[0]/c[3]*0.5+0.5)*W; SY[i]=(1-(c[1]/c[3]*0.5+0.5))*H; SV[i]=true; } }
  function projectAgents(MVP){ var W=canvas.width/DPR, H=canvas.height/DPR;
    for(var k=0;k<agentIdx.length;k++){ var i=agentIdx[k], p=P[i]; if(!p){SV[i]=false;continue;}
      var c=mApply(MVP,p[0],p[1],p[2]); if(c[3]<=0){SV[i]=false;continue;}
      SX[i]=(c[0]/c[3]*0.5+0.5)*W; SY[i]=(1-(c[1]/c[3]*0.5+0.5))*H; SV[i]=true; } }
  function esc(s){ return String(s).replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];}); }
  function enc(s){ return encodeURIComponent(String(s)).replace(/'/g,"%27"); }
  function placeLabels(){ var html="";
    for(var k=0;k<agentIdx.length;k++){ var i=agentIdx[k]; if(!SV[i])continue;
      var hot = (hover===i);
      html+="<span style='position:absolute;left:"+(SX[i]+11)+"px;top:"+(SY[i]-8)+"px;"+
        "font:"+(hot?"600 12px":"500 11px")+" system-ui,sans-serif;letter-spacing:.2px;"+
        "color:var(--fg,#e6edf3);opacity:"+(hot?"1":"0.55")+";"+
        "text-shadow:0 1px 3px var(--bg,#05080e),0 0 6px var(--bg,#05080e)'>"+esc(pretty(nodes[i].id))+"</span>"; }
    labelBox.innerHTML=html; }
  function pick(){ if(look||mX<0||!tip){ if(tip)tip.style.display="none"; return; }
    var best=-1, bd=14;
    for(var di=0;di<drawnIdx.length;di++){ var i=drawnIdx[di]; if(!SV[i])continue; var dx=SX[i]-mX, dy=SY[i]-mY, d=Math.sqrt(dx*dx+dy*dy); if(d<bd){ bd=d; best=i; } }
    hover=best; if(best<0){ tip.style.display="none"; return; }
    var n=nodes[best]; tip.style.display="block"; tip.style.left=(mX+14)+"px"; tip.style.top=(mY+14)+"px";
    tip.innerHTML = n.type==="agent" ? "<b>"+esc(pretty(n.id))+"</b><br><span style='color:var(--muted, #8aa0b6)'>agent</span>"
      : "<b>"+esc(n.title||n.id)+"</b><br><span style='color:var(--muted, #8aa0b6)'>"+esc(n.type)+"</span>"; }

  function pickAt(){ var best=-1,bd=16; for(var di=0;di<drawnIdx.length;di++){ var i=drawnIdx[di]; if(!SV[i])continue; var dx=SX[i]-mX,dy=SY[i]-mY,d=Math.sqrt(dx*dx+dy*dy); if(d<bd){bd=d;best=i;} } return best; }
  function panel(){ return document.getElementById("graph3dDetail"); }
  function openDetail(n){ if(!n)return; if(n.type==="agent") openAgent(n.id); else openNote(n.slug||n.id,n.cluster||""); }
  function openAgent(idFull){ var d=panel(); if(!d)return; var aid=idFull.replace("agent:",""); var list=MEM[aid]||[];
    var h="<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'><b style='color:var(--fg, #e7f3fb);font-size:13px'>"+esc(pretty(idFull))+"</b><span onclick='window.__g3dClose()' style='cursor:pointer;color:var(--muted, #8aa0b6);font-size:17px;line-height:1'>&times;</span></div>";
    h+="<div style='color:var(--muted, #8aa0b6);font-size:11px;margin-bottom:8px'>"+list.length+" memory notes &mdash; click one to read its .md</div>";
    for(var i=0;i<list.length;i++){ var nn=list[i], ns=nn.slug||nn.id, nc=nn.cluster||""; h+="<div onclick=\"window.__g3dNote(decodeURIComponent('"+enc(ns)+"'),decodeURIComponent('"+enc(nc)+"'))\" style='cursor:pointer;padding:3px 5px;border-radius:4px;color:var(--fg, #bcd3df);white-space:nowrap;overflow:hidden;text-overflow:ellipsis' onmouseover=\"this.style.background='color-mix(in srgb, var(--accent) 14%, transparent)'\" onmouseout=\"this.style.background='none'\">"+esc(nn.title||nn.id)+"</div>"; }
    d.innerHTML=h; d.scrollTop=0; d.style.display="block"; }
  function linkify(t,cluster){ return esc(t).replace(/\[\[([^\]|#]+)(\|[^\]]*)?\]\]/g,function(m,p1){ var s=p1.trim().toLowerCase().replace(/[^a-z0-9._-]/g,""); return "<span onclick=\"window.__g3dNote(decodeURIComponent('"+enc(s)+"'),decodeURIComponent('"+enc(cluster||"")+"'))\" style='color:var(--accent, #6fe0d0);cursor:pointer;text-decoration:underline'>"+esc(p1.trim())+"</span>"; }); }
  function openNote(slug,cluster){ var d=panel(); if(!d)return; d.style.display="block"; d.scrollTop=0; d.innerHTML="<div style='color:var(--muted, #8aa0b6)'>loading &hellip;</div>";
    fetch("/api/brain-note?slug="+encodeURIComponent(slug)+(cluster?"&cluster="+encodeURIComponent(cluster):"")+(window.__BRAIN_ID?"&brain="+encodeURIComponent(window.__BRAIN_ID):"")).then(function(r){return r.json();}).then(function(j){
      if(!j||j.error){ d.innerHTML="<div style='display:flex;justify-content:space-between'><span style='color:var(--err, var(--red, #e08a8a))'>"+esc((j&&j.error)||"not found")+"</span><span onclick='window.__g3dClose()' style='cursor:pointer;color:var(--muted, #8aa0b6)'>&times;</span></div>"; return; }
      var mh=""; (j.text||"").split(/\r?\n/).forEach(function(ln){
        if(/^#\s+/.test(ln)) mh+="<div style='font-weight:600;color:var(--fg, #e7f3fb);font-size:14px;margin:2px 0 7px'>"+linkify(ln.replace(/^#\s+/,""),j.cluster||"")+"</div>";
        else if(ln.trim()==="") mh+="<div style='height:6px'></div>";
        else mh+="<div style='margin:0 0 4px'>"+linkify(ln,j.cluster||"")+"</div>"; });
      d.innerHTML="<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:7px'><span style='color:var(--muted, #8aa0b6);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"+esc(j.cluster||"")+" &middot; "+esc(slug)+"</span><span onclick='window.__g3dClose()' style='cursor:pointer;color:var(--muted, #8aa0b6);font-size:17px;line-height:1;padding-left:8px'>&times;</span></div>"+mh;
      d.scrollTop=0;
    }).catch(function(){ d.innerHTML="<div style='color:var(--err, var(--red, #e08a8a))'>failed to load</div>"; }); }
  window.__g3dAgent=function(id){ openAgent(id); };
  window.__g3dNote=function(s,c){ openNote(s,c||""); };
  window.__g3dClose=function(){ var d=panel(); if(d) d.style.display="none"; };
  function resetView(){ eye=[0,760,2650]; yaw=0; pitch=-0.28; vel=[0,0,0]; }
  function bindEvents(){
    canvas.addEventListener("contextmenu",function(e){ e.preventDefault(); });
    canvas.addEventListener("mousedown",function(e){ look=true; lx=e.clientX; ly=e.clientY; downX=e.clientX; downY=e.clientY; moved=false; canvas.style.cursor="grabbing"; });
    window.addEventListener("mouseup",function(){ if(!moved && mX>=0){ if(lastMVP) project(lastMVP); var b=pickAt(); if(b>=0) openDetail(nodes[b]); } look=false; if(canvas)canvas.style.cursor="grab"; });
    canvas.addEventListener("mousemove",function(e){ var r=canvas.getBoundingClientRect(); mX=e.clientX-r.left; mY=e.clientY-r.top;
      if(look){ if(Math.abs(e.clientX-downX)+Math.abs(e.clientY-downY)>5) moved=true;
        yaw+=(e.clientX-lx)*0.004; pitch-=(e.clientY-ly)*0.004; pitch=Math.max(-1.45,Math.min(1.45,pitch)); lx=e.clientX; ly=e.clientY; } });
    canvas.addEventListener("mouseleave",function(){ mX=-1; mY=-1; });
    canvas.addEventListener("wheel",function(e){ e.preventDefault();
      var cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch), st=(e.deltaY<0?1:-1)*150;
      eye[0]+=cp*sy*st; eye[1]+=sp*st; eye[2]+=-cp*cy*st; },{passive:false});
    canvas.addEventListener("dblclick",function(){ spinOn=!spinOn; });
    window.addEventListener("keydown",function(e){ if(!canvas||canvas.offsetParent===null)return;
      var ae=document.activeElement; if(ae&&(ae.tagName==="INPUT"||ae.tagName==="TEXTAREA"||ae.isContentEditable))return;
      var k=e.key.toLowerCase();
      if(k==="r"){ resetView(); e.preventDefault(); return; }
      if(k===" "){ spinOn=!spinOn; e.preventDefault(); return; }
      if("wasdeq".indexOf(k)>=0 || k.indexOf("arrow")===0 || k==="shift"){ keys[k]=true; e.preventDefault(); } });
    window.addEventListener("keyup",function(e){ keys[e.key.toLowerCase()]=false; });
  }
  function start(){ if(raf)cancelAnimationFrame(raf); if(!t0)t0=performance.now()/1000; raf=requestAnimationFrame(frame); }
  function cleanup(){ if(raf)cancelAnimationFrame(raf); raf=0;
    ["graph3dCanvas","graph3dLabels","graph3dHud"].forEach(function(id){ var e=document.getElementById(id); if(e)e.remove(); });
    var c2=document.getElementById("graphCanvas"); if(c2)c2.style.display=""; inited=false; loadedData=null; }

  window.__graphShow = function(){
    try{
      if(!inited){ ensureDom(); gl=canvas.getContext("webgl")||canvas.getContext("experimental-webgl");
        if(!gl) throw new Error("no webgl");
        gl.disable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
        pProg=mkProg(PVS,PFS); lProg=mkProg(LVS,LFS); shProg=mkProg(SVS,SFS); resize(); buildScene(); loadedData=window.__BG; bindEvents();
        window.addEventListener("resize",function(){ if(canvas&&canvas.offsetParent!==null){ try{resize();}catch(e){} } });
        inited=true; }
      else if(loadedData!==window.__BG){ resize(); buildScene(); loadedData=window.__BG; }
      resize(); start();
    }catch(e){ try{ console.warn("3D galaxy unavailable, using 2D:",e); }catch(_){}
      cleanup(); if(window.__graphShow2D) window.__graphShow2D(); }
  };
  window.__graph3dReset = function(){ try { resetView(); } catch (_) {} };
})();
