# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private operator dashboard for the ingestion pipeline."""
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
<title>SeaCommons / Ingestion Operations</title>
<style>
:root{--bg:#071012;--panel:#0d181b;--line:#193036;--text:#d9e6e7;--muted:#6a858a;--mint:#61d7ad;--cyan:#61c8de;--amber:#efba62;--red:#ff6068}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{height:60px;border-bottom:1px solid var(--line);background:#091416;display:flex;align-items:center;justify-content:space-between;padding:0 22px;position:sticky;top:0;z-index:10}
.brand{display:flex;align-items:center;gap:11px;font:700 11px ui-monospace,monospace;letter-spacing:.14em;color:#8aa2a6}.logo{width:28px;height:28px;display:grid;place-items:center;background:#d3f2e5;color:#14352d;font-weight:900}.health{display:flex;gap:8px;align-items:center;font:700 10px ui-monospace,monospace;color:var(--mint)}.dot{width:7px;height:7px;border-radius:50%;background:var(--mint);box-shadow:0 0 12px var(--mint)}
main{max-width:1900px;margin:auto;padding:22px}.eyebrow{font:700 9px ui-monospace,monospace;letter-spacing:.15em;color:#637b80;text-transform:uppercase}.title{font-size:28px;font-weight:720;margin:4px 0 20px}
.cards{display:grid;grid-template-columns:repeat(8,minmax(115px,1fr));gap:9px}.card,.panel{border:1px solid var(--line);background:var(--panel);border-radius:4px}.card{padding:13px 14px;min-height:90px}.label{font:700 9px ui-monospace,monospace;letter-spacing:.09em;text-transform:uppercase;color:#668086}.value{font:700 25px ui-monospace,monospace;margin-top:12px}.raw .value{color:var(--cyan)}.parsed .value{color:var(--mint)}.analysis .value{color:var(--amber)}.live .value{color:var(--red)}
.flow{margin:16px 0;display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);background:var(--panel)}.stage{padding:12px 16px;border-right:1px solid var(--line);position:relative}.stage:last-child{border:0}.stage:not(:last-child):after{content:"›";position:absolute;right:-7px;top:15px;background:var(--panel);color:#49666c}.stage b{display:block;font:700 9px ui-monospace,monospace;letter-spacing:.1em;color:#6b858a}.stage span{display:block;font:700 18px ui-monospace,monospace;margin-top:6px}
.grid{display:grid;grid-template-columns:1.08fr 1fr 1fr;gap:11px}.panel{overflow:hidden}.panel h2{margin:0;padding:11px 13px;border-bottom:1px solid var(--line);font:700 10px ui-monospace,monospace;letter-spacing:.09em;text-transform:uppercase;display:flex;justify-content:space-between}.panel h2 span{color:#587378}.log{height:430px;overflow:auto}.row{padding:9px 12px;border-bottom:1px solid #14262a;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}.row:hover{background:#102024}.top{display:flex;justify-content:space-between;gap:10px}.kind{font-weight:700;color:var(--mint)}.rawRow .kind{color:var(--cyan)}.analysisRow .kind{color:var(--amber)}.time{color:#536f75;white-space:nowrap}.sub{margin-top:3px;color:#688287;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bottom{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:11px}.source-list{padding:6px 12px 12px}.source{display:grid;grid-template-columns:1fr auto;gap:10px;padding:8px 1px;border-bottom:1px solid #14262a;font:11px ui-monospace,monospace}.source small{color:#648086}.ok{color:var(--mint)!important}.warn{color:var(--amber)!important}.footer{display:flex;justify-content:space-between;margin-top:13px;color:#526d72;font:10px ui-monospace,monospace}
@media(max-width:1200px){.cards{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1fr}.bottom{grid-template-columns:1fr}}@media(max-width:650px){main{padding:12px}.cards{grid-template-columns:1fr 1fr}.flow{grid-template-columns:1fr}.stage{border-right:0;border-bottom:1px solid var(--line)}}
</style>
</head>
<body>
<header><div class="brand"><div class="logo">SC</div>SEACOMMONS / INGESTION OPERATIONS</div><div class="health"><i class="dot"></i><span id="health">CONNECTING</span></div></header>
<main>
<div class="eyebrow">Canonical pipeline / rolling 24 hours</div><div class="title">Operational ingestion field</div>
<section class="cards">
<div class="card live"><div class="label">Public Live</div><div class="value" id="live">–</div></div>
<div class="card raw"><div class="label">Raw observations</div><div class="value" id="raw">–</div></div>
<div class="card parsed"><div class="label">Parsed events</div><div class="value" id="parsed">–</div></div>
<div class="card analysis"><div class="label">Analysis outputs</div><div class="value" id="analyzed">–</div></div>
<div class="card"><div class="label">AIS fixes</div><div class="value" id="ais">–</div></div>
<div class="card"><div class="label">RF bursts</div><div class="value" id="rf">–</div></div>
<div class="card"><div class="label">Radio events</div><div class="value" id="radio">–</div></div>
<div class="card"><div class="label">Satellite obs.</div><div class="value" id="sat">–</div></div>
</section>
<section class="flow">
<div class="stage"><b>01 SOURCE INPUT</b><span id="fRaw">–</span></div><div class="stage"><b>02 PARSING</b><span id="fParsed">–</span></div><div class="stage"><b>03 ANALYSIS</b><span id="fAnalysis">–</span></div><div class="stage"><b>04 PUBLIC LIVE</b><span id="fLive">–</span></div>
</section>
<section class="grid">
<div class="panel"><h2>Raw ingestion <span id="rawFresh">latest –</span></h2><div class="log" id="rawLog"></div></div>
<div class="panel"><h2>Parsed events <span id="parsedFresh">latest –</span></h2><div class="log" id="eventLog"></div></div>
<div class="panel"><h2>Analysis engine <span id="analysisFresh">latest –</span></h2><div class="log" id="analysisLog"></div></div>
</section>
<section class="bottom">
<div class="panel"><h2>Source throughput <span>raw / 24h</span></h2><div class="source-list" id="sources"></div></div>
<div class="panel"><h2>Acquisition health <span>runtime</span></h2><div class="source-list" id="acquisition"></div></div>
</section>
<div class="footer"><span>Raw ≠ case. Analysis ≠ allegation. Live only receives publication-eligible signals.</span><span id="updated">–</span></div>
</main>
<script>
const $=id=>document.getElementById(id),fmt=n=>new Intl.NumberFormat("en").format(Number(n||0));
const ago=iso=>{if(!iso)return"–";let s=Math.max(0,(Date.now()-new Date(iso).getTime())/1000);return s<60?Math.round(s)+"s ago":s<3600?Math.round(s/60)+"m ago":Math.round(s/3600)+"h ago"};
async function get(p){let r=await fetch(p,{cache:"no-store",credentials:"same-origin"});if(!r.ok)throw new Error(r.status+" "+p);return r.json()}
function mkRow(x,cls){let d=document.createElement("div");d.className="row "+cls;let a=document.createElement("div");a.className="top";let k=document.createElement("span");k.className="kind";k.textContent=x.kind||x.observation_type||x.type||"event";let t=document.createElement("span");t.className="time";t.textContent=ago(x.received_at||x.updated_at||x.timestamp_utc||x.end_at);a.append(k,t);d.append(a);let s=document.createElement("div");s.className="sub";s.textContent=x.source_name||x.source||x.family||x.hypothesis_type||x.title||x.id||x.event_id||"";d.append(s);return d}
function renderLog(id,items,cls){let b=$(id);b.textContent="";(items||[]).slice(0,80).forEach(x=>b.append(mkRow(x,cls)));if(!b.childNodes.length){let d=document.createElement("div");d.className="row";d.textContent="No rows in current window";b.append(d)}}
function sourceRows(obj){let b=$("sources");b.textContent="";Object.entries(obj||{}).slice(0,20).forEach(([n,c])=>{let d=document.createElement("div");d.className="source";let x=document.createElement("b");x.textContent=n;let y=document.createElement("small");y.textContent=fmt(c);d.append(x,y);b.append(d)})}
function acquisitionRows(items){let b=$("acquisition");b.textContent="";(items||[]).forEach(x=>{let d=document.createElement("div");d.className="source";let n=document.createElement("b");n.textContent=x.label||x.family||x.source||"source";let s=document.createElement("small");let state=String(x.state||"unknown");s.textContent=state.toUpperCase();s.className=(state==="live"||state==="healthy")?"ok":state==="degraded"?"warn":"";d.append(n,s);b.append(d)})}
async function refresh(){try{
 let [o,r,e,a]=await Promise.all([get("/api/v1/operator/ingestion/overview?hours=24"),get("/api/v1/operator/ingestion/observations?hours=24&limit=80"),get("/api/v1/operator/ingestion/events?hours=24&limit=80"),get("/api/v1/operator/ingestion/analysis?hours=24&limit=80")]);
 let p=o.pipeline_status||{},q=p.pipeline||{},s=p.sensor_activity||{},l=p.live||{},f=p.freshness||{};
 [["live",l.total],["raw",q.raw_observations],["parsed",q.parsed_events],["analyzed",q.analysis_outputs],["ais",s.ais_fixes],["rf",s.radio_bursts],["radio",s.radio_events],["sat",s.satellite_observations],["fRaw",q.raw_observations],["fParsed",q.parsed_events],["fAnalysis",q.analysis_outputs],["fLive",l.total]].forEach(([id,v])=>$(id).textContent=fmt(v));
 $("rawFresh").textContent="latest "+ago(f.raw_observation);$("parsedFresh").textContent="latest "+ago(f.parsed_event);$("analysisFresh").textContent="latest "+ago(f.analysis_output);
 renderLog("rawLog",r.observations,"rawRow");renderLog("eventLog",e.events,"");renderLog("analysisLog",a.items,"analysisRow");sourceRows((o.source_observations||{}).by_source);acquisitionRows(o.acquisition);
 $("health").textContent="LIVE / AUTO-REFRESH 5S";$("health").style.color="var(--mint)";$("updated").textContent="updated "+new Date().toLocaleTimeString();
}catch(err){$("health").textContent="DEGRADED / "+String(err.message||err);$("health").style.color="var(--amber)"}}
refresh();setInterval(refresh,5000);
</script>
</body></html>"""
