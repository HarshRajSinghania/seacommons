# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private operator dashboard for the canonical SeaCommons OSINT pipeline."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.config import config

router = APIRouter(prefix="/api/v1/operator/ingestion", tags=["operator-ingestion"])


def _require_gateway(request: Request) -> None:
    expected = str(config.OPERATOR_GATEWAY_SECRET or "")
    if not expected:
        raise HTTPException(status_code=503, detail="Operator ingestion gateway is not configured")
    supplied = request.headers.get("x-seacommons-operator-gateway", "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Operator gateway authentication required")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request) -> HTMLResponse:
    _require_gateway(request)
    return HTMLResponse(_HTML)


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SeaCommons / OSINT Operations</title>
<style>
:root{--bg:#071012;--panel:#0d181b;--panel2:#0a1417;--line:#193036;--text:#d9e6e7;--muted:#6a858a;--mint:#61d7ad;--cyan:#61c8de;--amber:#efba62;--red:#ff6068;--violet:#bda7ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{height:60px;border-bottom:1px solid var(--line);background:#091416;display:flex;align-items:center;justify-content:space-between;padding:0 22px;position:sticky;top:0;z-index:10}
.brand{display:flex;align-items:center;gap:11px;font:700 11px ui-monospace,monospace;letter-spacing:.14em;color:#8aa2a6}.logo{width:28px;height:28px;display:grid;place-items:center;background:#d3f2e5;color:#14352d;font-weight:900}.health{display:flex;gap:8px;align-items:center;font:700 10px ui-monospace,monospace;color:var(--mint);max-width:58%;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}.dot{width:7px;height:7px;border-radius:50%;background:var(--mint);box-shadow:0 0 12px var(--mint);flex:none}
main{max-width:1920px;margin:auto;padding:22px}.eyebrow{font:700 9px ui-monospace,monospace;letter-spacing:.15em;color:#637b80;text-transform:uppercase}.title{font-size:28px;font-weight:720;margin:4px 0 6px}.subtitle{font-size:12px;color:#6f898e;max-width:1000px;margin-bottom:20px}
.panel{border:1px solid var(--line);background:var(--panel);border-radius:4px;overflow:hidden}.panel h2{margin:0;padding:11px 13px;border-bottom:1px solid var(--line);font:700 10px ui-monospace,monospace;letter-spacing:.09em;text-transform:uppercase;display:flex;justify-content:space-between}.panel h2 span{color:#587378}
.funnel{display:grid;grid-template-columns:repeat(8,1fr);border:1px solid var(--line);background:var(--panel);margin-bottom:12px}.stage{padding:13px 12px;min-height:92px;border-right:1px solid var(--line);position:relative}.stage:last-child{border:0}.stage:not(:last-child):after{content:"›";position:absolute;right:-7px;top:17px;background:var(--panel);color:#49666c}.stage .n{font:700 23px ui-monospace,monospace;margin-top:13px}.stage .l{font:700 9px ui-monospace,monospace;letter-spacing:.08em;color:#69848a;text-transform:uppercase}.stage:nth-child(1) .n{color:var(--cyan)}.stage:nth-child(2) .n{color:var(--mint)}.stage:nth-child(3) .n,.stage:nth-child(4) .n{color:var(--amber)}.stage:nth-child(6) .n,.stage:nth-child(7) .n{color:var(--violet)}.stage:nth-child(8) .n{color:var(--red)}
.sensors{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px}.overallHead{display:flex;justify-content:space-between;align-items:end;margin:18px 0 8px}.overallHead .title{font-size:19px;margin:0}.overallMeta{font:10px ui-monospace,monospace;color:#587378}.overallBreak{display:grid;grid-template-columns:repeat(4,1fr);gap:11px;margin-bottom:12px}.overallList{padding:7px 12px 12px;max-height:270px;overflow:auto}.sensor{border:1px solid var(--line);background:var(--panel2);padding:10px 12px}.sensor b{display:block;font:700 9px ui-monospace,monospace;letter-spacing:.08em;color:#658087}.sensor span{display:block;font:700 17px ui-monospace,monospace;margin-top:6px}
.fleetPanel{margin:0 0 12px}.fleetGrid{display:grid;grid-template-columns:repeat(4,1fr);gap:0}.fleetAsset{padding:9px 12px;border-right:1px solid #14262a;border-bottom:1px solid #14262a;min-width:0}.fleetAsset b{display:block;font:700 10px ui-monospace,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.fleetAsset small{display:block;color:#607a80;font:9px/1.45 ui-monospace,monospace;margin-top:3px}.fleetState{color:var(--mint)!important}.fleetState.stale{color:var(--amber)!important}.fleetState.offline{color:#6a858a!important}
.logs{display:grid;grid-template-columns:1fr 1fr 1fr;gap:11px}.log{height:330px;overflow:auto}.row{padding:9px 12px;border-bottom:1px solid #14262a;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.row:hover{background:#102024}.top{display:flex;justify-content:space-between;gap:10px}.kind{font-weight:700;color:var(--mint)}.rawRow .kind{color:var(--cyan)}.analysisRow .kind{color:var(--amber)}.time{color:#536f75;white-space:nowrap}.sub{margin-top:3px;color:#688287;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.detail{margin-top:3px;color:#829a9e;font-size:10px}
.casegrid{display:grid;grid-template-columns:1.35fr .65fr;gap:11px;margin-top:11px}.cases{max-height:660px;overflow:auto}.case{padding:13px;border-bottom:1px solid #14262a}.casehead{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.casetitle{font:700 12px ui-monospace,monospace;color:#d8e7e8}.badge{font:700 9px ui-monospace,monospace;border:1px solid #315057;padding:3px 6px;border-radius:3px;color:#91a8ac;white-space:nowrap}.badge.collecting{color:var(--amber)}.badge.corroborated,.badge.review_ready{color:var(--violet)}.case p{font-size:11px;line-height:1.55;color:#7f999e;margin:7px 0}.chips{display:flex;flex-wrap:wrap;gap:5px;margin:7px 0}.chip{font:700 9px ui-monospace,monospace;border:1px solid #203a40;padding:3px 5px;color:#799297}.evidence{font:10px/1.5 ui-monospace,monospace;color:#657f84}.ev{padding:4px 0;border-top:1px dotted #183036}.lineage{color:var(--cyan)}.blocker{color:var(--amber)}.legal{color:#607a80;font-style:italic}
.pipelineMap{max-height:660px;overflow:auto}.chain{padding:12px;border-bottom:1px solid #14262a}.chain b{font:700 10px ui-monospace,monospace;color:#cfe0e2}.chain .path{font:10px/1.5 ui-monospace,monospace;color:#6c878c;margin-top:6px}.chain .controls{font-size:10px;line-height:1.5;color:#557075;margin-top:6px}
.bottom{display:grid;grid-template-columns:1fr 1fr 1fr;gap:11px;margin-top:11px}.source-list{padding:6px 12px 12px;max-height:330px;overflow:auto}.source{display:grid;grid-template-columns:1fr auto;gap:10px;padding:7px 1px;border-bottom:1px solid #14262a;font:10px ui-monospace,monospace}.source small{color:#648086}.ok{color:var(--mint)!important}.warn{color:var(--amber)!important}.diag{padding:9px 12px;font:10px/1.55 ui-monospace,monospace;color:#6d898e}.diag strong{color:#a8bdc1}
.footer{display:flex;justify-content:space-between;margin-top:13px;color:#526d72;font:10px ui-monospace,monospace}
@media(max-width:1300px){.funnel{grid-template-columns:repeat(4,1fr)}.sensors{grid-template-columns:repeat(3,1fr)}.overallBreak{grid-template-columns:1fr 1fr}.fleetGrid{grid-template-columns:repeat(2,1fr)}.logs{grid-template-columns:1fr}.casegrid{grid-template-columns:1fr}.bottom{grid-template-columns:1fr}}@media(max-width:650px){main{padding:12px}.funnel{grid-template-columns:1fr 1fr}.sensors{grid-template-columns:1fr 1fr}.overallBreak{grid-template-columns:1fr}.fleetGrid{grid-template-columns:1fr}.health{max-width:45%}}
</style>
</head>
<body>
<header><div class="brand"><div class="logo">SC</div>SEACOMMONS / OSINT OPERATIONS</div><div class="health"><i class="dot"></i><span id="health">CONNECTING</span></div></header>
<main>
<div class="eyebrow">Canonical evidence pipeline / rolling 24 hours</div>
<div class="title">Operational intelligence field</div>
<div class="subtitle">Every number is a different evidentiary layer. High collection volume is not presented as a high case count. Investigation hypotheses are not findings of illegality.</div>
<section class="funnel" id="funnel"></section>
<section class="sensors">
<div class="sensor"><b>AIS FIXES</b><span id="ais">–</span></div><div class="sensor"><b>RF BURSTS</b><span id="rf">–</span></div><div class="sensor"><b>RADIO EVENTS</b><span id="radio">–</span></div><div class="sensor"><b>SATELLITE OBS.</b><span id="sat">–</span></div><div class="sensor"><b>ACTIVE SOURCE NAMES</b><span id="activeSources">–</span></div>
</section>
<section class="panel fleetPanel"><h2>Civil + state SAR fleet <span id="fleetFresh">AIS freshness / operational context</span></h2><div class="fleetGrid" id="sarFleet"></div></section>
<div class="overallHead"><div><div class="eyebrow">Historical storage / all retained data</div><div class="title">All-time corpus</div></div><div class="overallMeta" id="overallFresh">loading historical inventory…</div></div>
<section class="funnel" id="overallFunnel"></section>
<section class="sensors">
<div class="sensor"><b>TOTAL AIS FIXES</b><span id="overallAis">–</span></div><div class="sensor"><b>TOTAL RF BURSTS</b><span id="overallRf">–</span></div><div class="sensor"><b>TOTAL RADIO EVENTS</b><span id="overallRadio">–</span></div><div class="sensor"><b>TOTAL SATELLITE OBS.</b><span id="overallSat">–</span></div><div class="sensor"><b>SOURCE NAMES EVER SEEN</b><span id="overallSources">–</span></div>
</section>
<section class="overallBreak">
<div class="panel"><h2>Historical sources <span>raw corpus</span></h2><div class="overallList" id="overallRawSources"></div></div>
<div class="panel"><h2>Raw observation types <span>all-time</span></h2><div class="overallList" id="overallRawTypes"></div></div>
<div class="panel"><h2>Normalized event types <span>all-time</span></h2><div class="overallList" id="overallEventTypes"></div></div>
<div class="panel"><h2>Analysis corpus <span>episodes + hypotheses</span></h2><div class="overallList" id="overallAnalysis"></div></div>
</section>
<section class="logs">
<div class="panel"><h2>01 Raw ingestion <span id="rawFresh">latest –</span></h2><div class="log" id="rawLog"></div></div>
<div class="panel"><h2>02 Normalized / parsed <span id="parsedFresh">latest –</span></h2><div class="log" id="eventLog"></div></div>
<div class="panel"><h2>03 Episodes + hypotheses <span id="analysisFresh">latest –</span></h2><div class="log" id="analysisLog"></div></div>
</section>
<section class="casegrid">
<div class="panel"><h2>Real investigation chains <span>actual DB / no synthetic cases</span></h2><div class="cases" id="cases"></div></div>
<div class="panel"><h2>Parser topology <span>source → evidence → case</span></h2><div class="pipelineMap" id="pipelineMap"></div></div>
</section>
<section class="bottom">
<div class="panel"><h2>Source throughput <span>raw / 24h</span></h2><div class="source-list" id="sources"></div></div>
<div class="panel"><h2>Acquisition health <span>runtime</span></h2><div class="source-list" id="acquisition"></div></div>
<div class="panel"><h2>Why signals stop <span>current funnel</span></h2><div class="diag" id="diagnostics"></div></div>
</section>
<div class="footer"><span>Raw ≠ event ≠ cue ≠ investigation ≠ corroboration ≠ illegality.</span><span id="updated">–</span></div>
</main>
<script>
const $=id=>document.getElementById(id),fmt=n=>new Intl.NumberFormat("en").format(Number(n||0));
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const ago=iso=>{if(!iso)return"–";let s=Math.max(0,(Date.now()-new Date(iso).getTime())/1000);return s<60?Math.round(s)+"s ago":s<3600?Math.round(s/60)+"m ago":Math.round(s/3600)+"h ago"};
async function get(p){let r=await fetch(p,{cache:"no-store",credentials:"same-origin"});if(!r.ok)throw new Error(r.status+" "+p);return r.json()}
function rawLabel(x){if(x.observation_type==="ais_gap")return"AIS reappearance after silence";return x.observation_type||x.kind||"source observation"}
function eventLabel(x){let m=x.metadata||{};if(x.source==="ais"&&x.type==="ais_anomaly"&&m.anomaly_type==="gap")return"AIS telemetry gap cue";if(x.source==="mda"&&x.type==="ais_anomaly"&&["gap","long_gap"].includes(m.anomaly_type))return"MDA dark-gap cue";return [x.type,m.anomaly_type].filter(Boolean).join(" / ")||"parsed event"}
function mkRow(x,cls,mode){let d=document.createElement("div");d.className="row "+cls;let a=document.createElement("div");a.className="top";let k=document.createElement("span");k.className="kind";k.textContent=mode==="raw"?rawLabel(x):mode==="event"?eventLabel(x):(x.kind||x.family||x.hypothesis_type||"analysis");let t=document.createElement("span");t.className="time";t.textContent=ago(x.received_at||x.updated_at||x.timestamp_utc||x.end_at);a.append(k,t);d.append(a);let s=document.createElement("div");s.className="sub";s.textContent=x.source_name||x.source||x.title||x.family||x.hypothesis_type||x.id||x.event_id||"";d.append(s);if(mode==="raw"&&x.observation_type==="ais_gap"){let q=document.createElement("div");q.className="detail";q.textContent="Telemetry: vessel reappeared after sampled silence. Not a dark-activity finding.";d.append(q)}return d}
function renderLog(id,items,cls,mode){let b=$(id);b.textContent="";(items||[]).slice(0,80).forEach(x=>b.append(mkRow(x,cls,mode)));if(!b.childNodes.length){let d=document.createElement("div");d.className="row";d.textContent="No rows in current window";b.append(d)}}
function renderFunnel(f){let b=$("funnel");b.textContent="";(f.stages||[]).forEach(s=>{let d=document.createElement("div");d.className="stage";let l=document.createElement("div");l.className="l";l.textContent=s.label;let n=document.createElement("div");n.className="n";n.textContent=fmt(s.count);d.append(l,n);b.append(d)})}
function kvRows(id,obj,limit=24){let b=$(id);b.textContent="";Object.entries(obj||{}).slice(0,limit).forEach(([n,c])=>{let d=document.createElement("div");d.className="source";let x=document.createElement("b");x.textContent=n;let y=document.createElement("small");y.textContent=fmt(c);d.append(x,y);b.append(d)})}function sourceRows(obj){kvRows("sources",obj,24)}
function acquisitionRows(items){let b=$("acquisition");b.textContent="";(items||[]).forEach(x=>{let d=document.createElement("div");d.className="source";let n=document.createElement("b");n.textContent=x.label||x.family||x.source||"source";let s=document.createElement("small");let state=String(x.state||"unknown");s.textContent=state.toUpperCase();s.className=(state==="live"||state==="healthy")?"ok":state==="degraded"?"warn":"";d.append(n,s);b.append(d)})}
function renderCases(items){let b=$("cases");b.textContent="";(items||[]).forEach(c=>{let d=document.createElement("div");d.className="case";let h=document.createElement("div");h.className="casehead";let t=document.createElement("div");t.className="casetitle";t.textContent=c.label+" · "+(c.episode_id||c.case_id);let badge=document.createElement("span");badge.className="badge "+c.state;badge.textContent=(c.state||"unknown")+" / "+(c.evidence_stage||"");h.append(t,badge);d.append(h);let p=document.createElement("p");p.textContent=c.possible_meaning;d.append(p);let chips=document.createElement("div");chips.className="chips";[...(c.independence_groups||[]).map(x=>"lineage: "+x),...(c.reason_codes||[]).map(x=>"reason: "+x),...(c.blockers||[]).slice(0,4).map(x=>"stop: "+x)].forEach(x=>{let q=document.createElement("span");q.className="chip";q.textContent=x;chips.append(q)});d.append(chips);let ev=document.createElement("div");ev.className="evidence";(c.evidence||[]).slice(0,6).forEach(e=>{let q=document.createElement("div");q.className="ev";q.innerHTML="<span class='lineage'>"+esc(e.lineage)+"</span> · "+esc(e.title||e.id)+" · "+esc(e.anomaly_type||e.type||e.kind||"");ev.append(q)});d.append(ev);let lg=document.createElement("p");lg.className="legal";lg.textContent=c.illegal_activity_status;d.append(lg);b.append(d)});if(!b.childNodes.length){b.innerHTML="<div class='case'>No investigation hypotheses in the selected window.</div>"}}
function renderMap(chains){let b=$("pipelineMap");b.textContent="";(chains||[]).forEach(c=>{let d=document.createElement("div");d.className="chain";let h=document.createElement("b");h.textContent=c.source_family;let p=document.createElement("div");p.className="path";p.textContent=(c.raw_observations||[]).join(" + ")+" → "+(c.normalized_outputs||[]).join(" + ")+" → "+(c.case_families||[]).join(" / ");let q=document.createElement("div");q.className="controls";q.textContent="Controls: "+(c.false_positive_controls||[]).join(" · ");d.append(h,p,q);b.append(d)})}
function renderFleet(data){let b=$("sarFleet");b.textContent="";let rows=(data.features||[]).map(f=>{let p=f.properties||{},ts=p.position_timestamp_utc||p.last_seen_utc||p.last_seen||p.timestamp||p.updated_at,state=p.ais_status||"offline";return{p,ts,state}}).sort((a,b)=>({live:0,stale:1,offline:2}[a.state]-({live:0,stale:1,offline:2}[b.state])||String(a.p.ship_name||"").localeCompare(String(b.p.ship_name||"")));rows.forEach(x=>{let d=document.createElement("div");d.className="fleetAsset";let n=document.createElement("b");n.textContent=x.p.ship_name||x.p.vessel_name||x.p.mmsi||"SAR asset";let o=document.createElement("small");o.textContent=(x.p.org||x.p.organisation||x.p.operator_type||"")+" · "+(x.p.operator_type==="state_authority"?"state SAR":"civil SAR");let st=document.createElement("small");st.className="fleetState "+x.state;st.textContent=x.state.toUpperCase()+" · AIS "+(x.ts?ago(x.ts):"not available")+(x.p.position_policy==="stale_withheld"?" · map position withheld":"");d.append(n,o,st);b.append(d)});if(!rows.length)b.innerHTML="<div class='fleetAsset'>No SAR registry rows available.</div>";let live=rows.filter(x=>x.state==="live").length,stale=rows.filter(x=>x.state==="stale").length,tr=data.meta?.tracking_stream||{};$("fleetFresh").textContent="current fixes only · "+live+" live · "+stale+" stale · "+(rows.length-live-stale)+" offline · tracker "+(tr.connected?"connected":"degraded")}
async function refreshFleet(){try{renderFleet(await get("/api/v1/operator/ingestion/sar-fleet"))}catch(err){$("fleetFresh").textContent="fleet inventory degraded · "+String(err.message||err)}}
function renderDiagnostics(f){let d=f.diagnostics||{},b=$("diagnostics");let top=(o,n=8)=>Object.entries(o||{}).slice(0,n).map(([k,v])=>esc(k)+": <strong>"+fmt(v)+"</strong>").join("<br>");b.innerHTML="<strong>Hypothesis states</strong><br>"+top(d.hypothesis_states)+"<br><br><strong>Evidence stages</strong><br>"+top(d.hypothesis_evidence_stages)+"<br><br><strong>Episode verification</strong><br>"+top(d.episode_verification)+"<br><br><strong>Counter-indicators</strong><br>"+top(d.hypothesis_counter_indicators)}
async function refreshOverall(){try{
 let o=await get("/api/v1/operator/ingestion/overall"),s=o.sensor_activity||{},b=o.breakdowns||{},f=o.first_last||{};
 let fake={stages:o.stages||[]};let box=$("overallFunnel");box.textContent="";(fake.stages||[]).forEach(x=>{let d=document.createElement("div");d.className="stage";let l=document.createElement("div");l.className="l";l.textContent=x.label;let n=document.createElement("div");n.className="n";n.textContent=fmt(x.count);d.append(l,n);box.append(d)});
 $("overallAis").textContent=fmt(s.ais_fixes);$("overallRf").textContent=fmt(s.radio_bursts);$("overallRadio").textContent=fmt(s.radio_events);$("overallSat").textContent=fmt(s.satellite_observations);$("overallSources").textContent=fmt(s.source_names);
 kvRows("overallRawSources",b.raw_by_source,30);kvRows("overallRawTypes",b.raw_observation_types,30);kvRows("overallEventTypes",b.normalized_event_types,30);
 let analysis={};Object.entries(b.episode_families||{}).forEach(([k,v])=>analysis["episode · "+k]=v);Object.entries(b.hypothesis_types||{}).forEach(([k,v])=>analysis["hypothesis · "+k]=v);kvRows("overallAnalysis",analysis,40);
 let from=f.raw_first?new Date(f.raw_first).toLocaleDateString():"–";$("overallFresh").textContent="stored since "+from+" · cached "+fmt(o.cache_ttl_seconds)+"s · updated "+new Date(o.generated_at).toLocaleTimeString();
}catch(err){$("overallFresh").textContent="historical inventory degraded · "+String(err.message||err)}}
async function refresh(){try{
 let [o,f,r,e,a,c,m]=await Promise.all([
  get("/api/v1/operator/ingestion/overview?hours=24"),get("/api/v1/operator/ingestion/funnel?hours=24"),get("/api/v1/operator/ingestion/observations?hours=24&limit=80"),get("/api/v1/operator/ingestion/events?hours=24&limit=80"),get("/api/v1/operator/ingestion/analysis?hours=24&limit=80"),get("/api/v1/operator/ingestion/cases?hours=168&limit=20"),get("/api/v1/operator/ingestion/pipeline-map")
 ]);
 let p=o.pipeline_status||{},s=p.sensor_activity||{},fr=p.freshness||{};
 renderFunnel(f);$("ais").textContent=fmt(s.ais_fixes);$("rf").textContent=fmt(s.radio_bursts);$("radio").textContent=fmt(s.radio_events);$("sat").textContent=fmt(s.satellite_observations);$("activeSources").textContent=fmt(s.active_source_names);
 $("rawFresh").textContent="latest "+ago(fr.raw_observation);$("parsedFresh").textContent="latest "+ago(fr.parsed_event);$("analysisFresh").textContent="latest "+ago(fr.analysis_output);
 renderLog("rawLog",r.observations,"rawRow","raw");renderLog("eventLog",e.events,"","event");renderLog("analysisLog",a.items,"analysisRow","analysis");renderCases(c.cases);renderMap(m.chains);renderDiagnostics(f);sourceRows((o.source_observations||{}).by_source);acquisitionRows(o.acquisition);
 $("health").textContent="LIVE / AUTO-REFRESH 5S";$("health").style.color="var(--mint)";$("updated").textContent="updated "+new Date().toLocaleTimeString();
}catch(err){$("health").textContent="DEGRADED / "+String(err.message||err);$("health").style.color="var(--amber)"}}
refresh();refreshOverall();refreshFleet();setInterval(refresh,5000);setInterval(refreshFleet,15000);setInterval(refreshOverall,60000);
</script>
</body></html>"""
