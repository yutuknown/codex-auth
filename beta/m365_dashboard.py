"""Operator-only dashboard helpers for the hosted M365 beta.

The dashboard session is a short-lived HMAC token derived from the configured
admin key.  The raw key is never placed in a cookie or returned to the client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from fastapi import Request

DASHBOARD_COOKIE = "codex_auth_m365_beta_dashboard"
DASHBOARD_SESSION_TTL = 8 * 60 * 60


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _session_secret() -> bytes:
    value = (
        os.environ.get("CODEX_AUTH_M365_BETA_DASHBOARD_SESSION_KEY")
        or os.environ.get("CODEX_AUTH_M365_BETA_ADMIN_KEY")
        or os.environ.get("CODEX_AUTH_M365_BETA_API_KEY")
        or ""
    )
    return value.encode()


def issue_dashboard_session(now: int | None = None) -> str:
    issued = int(now or time.time())
    payload = f"{issued}.{secrets.token_urlsafe(18)}"
    signature = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64url(signature)}"


def dashboard_session_valid(value: str, now: int | None = None) -> bool:
    if not value or not _session_secret():
        return False
    try:
        issued_raw, nonce, supplied = value.split(".", 2)
        issued = int(issued_raw)
    except (TypeError, ValueError):
        return False
    current = int(now or time.time())
    if issued > current + 60 or current - issued > DASHBOARD_SESSION_TTL:
        return False
    payload = f"{issued}.{nonce}"
    expected = _b64url(hmac.new(_session_secret(), payload.encode(), hashlib.sha256).digest())
    return secrets.compare_digest(supplied, expected)


def dashboard_request_authorized(request: Request) -> bool:
    return dashboard_session_valid(request.cookies.get(DASHBOARD_COOKIE, ""))


def dashboard_html() -> str:
    """Return the self-contained dashboard shell; all data loads after login."""

    return r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Auth · M365 Beta</title><style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#12161b;--panel2:#171c22;--line:#2a313a;--text:#f4f7fa;--muted:#9ca7b3;--green:#29d69b;--amber:#ffbf47;--red:#ff6874;--blue:#67a9ff;--violet:#b69cff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 75% 0,#18233a 0,transparent 32%),var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,sans-serif}button,input,textarea{font:inherit}.hidden{display:none!important}
.login{min-height:100vh;display:grid;place-items:center;padding:24px}.login-card{width:min(430px,100%);padding:32px;border:1px solid var(--line);border-radius:20px;background:rgba(18,22,27,.94);box-shadow:0 28px 80px #0008}.brand{font-size:22px;font-weight:750}.brand span{font-weight:400;color:var(--muted)}
input,textarea{width:100%;border:1px solid var(--line);border-radius:10px;background:#0d1116;color:var(--text);padding:11px 12px;outline:none}input:focus,textarea:focus{border-color:var(--blue)}textarea{min-height:190px;resize:vertical}.btn{border:1px solid var(--line);border-radius:10px;background:#20262e;color:var(--text);padding:10px 14px;cursor:pointer}.btn:hover{border-color:#4a5665}.btn.primary{background:var(--text);color:#0b0d10;border-color:var(--text);font-weight:700}.btn.blue{background:#1768d5;border-color:#2f7ce3}.btn.danger{color:#ff9da5}.error{color:var(--red);min-height:20px}.muted{color:var(--muted)}
.app{min-height:100vh;display:grid;grid-template-columns:240px 1fr}.side{border-right:1px solid var(--line);padding:24px 16px;position:sticky;top:0;height:100vh;background:#0d1014}.nav{margin-top:32px;display:grid;gap:6px}.nav button{background:transparent;border:0;color:var(--muted);text-align:left;padding:11px 12px;border-radius:9px;cursor:pointer}.nav button.active,.nav button:hover{background:#20252c;color:var(--text)}.side-bottom{position:absolute;left:16px;right:16px;bottom:20px}
.main{padding:28px 34px 64px;max-width:1500px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:26px}.title{font-size:25px;font-weight:760}.badge{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}.dot{width:8px;height:8px;border-radius:50%;background:var(--amber)}.ok .dot{background:var(--green)}.bad .dot{background:var(--red)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.card{border:1px solid var(--line);border-radius:15px;background:rgba(18,22,27,.88);padding:18px;min-width:0}.metric{font-size:25px;font-weight:760;margin-top:7px}.label{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.section{margin-top:22px}.section-head{display:flex;justify-content:space-between;align-items:end;margin-bottom:12px}.section h2{margin:0;font-size:18px}.two{display:grid;grid-template-columns:1.15fr .85fr;gap:14px}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:14px}.notice{border-left:3px solid var(--amber);background:#241c0d;padding:12px 14px;border-radius:8px;color:#ffd88a}.notice.blue{border-color:var(--blue);background:#111d30;color:#b7d6ff}.kv{display:grid;grid-template-columns:160px 1fr;gap:8px 14px;margin-top:14px}.kv div:nth-child(odd){color:var(--muted)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}.state{font-size:12px;border:1px solid var(--line);padding:3px 7px;border-radius:999px;white-space:nowrap}.state.verified_live,.state.active{color:var(--green)}.state.blocked_by_upstream,.state.expiring_soon{color:var(--amber)}.state.unsupported,.state.re_import_required{color:var(--red)}
.logs{height:390px;overflow:auto;background:#090c0f;border:1px solid var(--line);border-radius:12px;padding:12px;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:#b8c2cc}.log{padding:5px 0;border-bottom:1px solid #171c22}.view{display:none}.view.active{display:block}.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}.file{position:relative;overflow:hidden}.file input{position:absolute;inset:0;opacity:0;cursor:pointer}
@media(max-width:1000px){.grid{grid-template-columns:repeat(2,1fr)}.two,.split{grid-template-columns:1fr}}@media(max-width:720px){.app{grid-template-columns:1fr}.side{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line)}.nav{grid-template-columns:repeat(3,1fr);margin-top:18px}.side-bottom{position:static;margin-top:14px}.main{padding:22px 16px}.grid{grid-template-columns:1fr 1fr}}
</style></head><body>
<section id="login" class="login"><div class="login-card"><div class="brand">codex<span>-auth</span></div><h1>M365 Beta Dashboard</h1><p class="muted">Operator access uses the beta admin key. The key is exchanged for a short-lived HttpOnly session and is never stored in browser storage.</p><input id="loginKey" type="password" autocomplete="current-password" placeholder="Admin key"><div class="actions"><button class="btn primary" id="loginButton">Sign in</button></div><p id="loginError" class="error"></p></div></section>
<section id="app" class="app hidden"><aside class="side"><div class="brand">codex<span>-auth</span></div><div class="muted">M365 bearer beta</div><nav class="nav"><button data-view="overview" class="active">Overview</button><button data-view="account">Account</button><button data-view="models">Models</button><button data-view="capabilities">Capabilities</button><button data-view="verification">Verification</button><button data-view="logs">Live logs</button></nav><div class="side-bottom"><button id="logout" class="btn danger" style="width:100%">Sign out</button></div></aside>
<main class="main"><header class="top"><div><div id="pageTitle" class="title">Overview</div><div id="build" class="muted"></div></div><div id="connectionBadge" class="badge"><span class="dot"></span><span>Loading</span></div></header>
<div id="overview" class="view active"><div class="grid"><div class="card"><div class="label">Generation</div><div id="generation" class="metric">—</div></div><div class="card"><div class="label">Credential</div><div id="credentialState" class="metric">—</div></div><div class="card"><div class="label">Models</div><div id="modelCount" class="metric">—</div></div><div class="card"><div class="label">Persistence</div><div id="persistence" class="metric">—</div></div></div><section class="section two"><div class="card"><h2>Runtime</h2><div id="runtimeKv" class="kv"></div><div class="actions"><button class="btn blue" id="probe">Run safe connection test</button><button class="btn" id="refresh">Refresh credential</button></div><p id="actionStatus" class="muted"></p></div><div class="card"><h2>Deployment readiness</h2><div id="readiness"></div></div></section></div>
<div id="account" class="view"><section class="section two"><div class="card"><h2>Connect Microsoft 365</h2><p class="notice blue">Microsoft's M365 first-party clients reject device-code login and redirect browser authorization to Microsoft’s own landing page. This dashboard can open that sign-in, but browser isolation prevents Render from reading its token response automatically.</p><div class="actions"><button class="btn blue" id="openMicrosoft">Open Microsoft 365 sign-in</button></div><ol class="muted"><li>Sign in to Microsoft 365 in the opened tab.</li><li>Copy the successful OAuth token response JSON from DevTools.</li><li>Paste it below or select the JSON file. Route and refresh metadata are derived and preserved server-side.</li></ol><textarea id="credentialJson" spellcheck="false" autocomplete="off" placeholder='{"token_type":"Bearer", ...}'></textarea><div class="actions"><label class="btn file">Choose JSON file<input id="credentialFile" type="file" accept="application/json,.json"></label><button class="btn primary" id="importCredential">Import / Replace</button></div><p id="importStatus" class="muted"></p></div><div class="card"><h2>Credential health</h2><div id="credentialKv" class="kv"></div><p class="notice">Runtime imports survive restart only when encrypted external Postgres is configured. The dashboard never returns access or refresh tokens.</p></div></section></div>
<div id="models" class="view"><section class="section card"><div class="section-head"><h2>Available models</h2><span class="muted">Catalog source is shown per model</span></div><div style="overflow:auto"><table><thead><tr><th>Model</th><th>Name</th><th>Source</th><th>Reasoning</th></tr></thead><tbody id="modelsBody"></tbody></table></div></section></div>
<div id="capabilities" class="view"><section class="section card"><div class="section-head"><h2>Capability evidence</h2><span class="muted">No mock-only promotion</span></div><div style="overflow:auto"><table><thead><tr><th>Feature</th><th>State</th><th>Comparison</th><th>Evidence</th></tr></thead><tbody id="capabilitiesBody"></tbody></table></div></section></div>
<div id="verification" class="view"><section class="section split"><div class="card"><h2>Commit-bound verification</h2><div id="verificationKv" class="kv"></div></div><div class="card"><h2>Metrics</h2><div id="metricsKv" class="kv"></div></div></section></div>
<div id="logs" class="view"><section class="section"><div class="section-head"><h2>Redacted runtime events</h2><span class="muted">Prompts, responses, tokens, URLs, and identities are excluded</span></div><div id="logBox" class="logs"></div></section></div>
</main></section>
<script>
const $=id=>document.getElementById(id), esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let state=null,events=null;
async function api(path,options={}){const response=await fetch(path,{...options,headers:{'content-type':'application/json',...(options.headers||{})}});let data={};try{data=await response.json()}catch{}if(response.status===401){showLogin();throw new Error('Dashboard session expired')}if(!response.ok)throw new Error(data.error||data.detail||'Request failed');return data}
function showLogin(){$('app').classList.add('hidden');$('login').classList.remove('hidden');if(events){events.close();events=null}}
function showApp(){$('login').classList.add('hidden');$('app').classList.remove('hidden')}
function kv(target,items){target.innerHTML=Object.entries(items).map(([k,v])=>`<div>${esc(k.replaceAll('_',' '))}</div><div>${esc(v??'Unavailable')}</div>`).join('')}
function badge(ok,text){$('connectionBadge').className='badge '+(ok?'ok':'bad');$('connectionBadge').innerHTML=`<span class="dot"></span><span>${esc(text)}</span>`}
async function load(){state=await api('/dashboard/api/overview');showApp();const c=state.credential||{},p=c.credential_persistence||{},models=state.models?.data||[],ready=state.readiness||{};$('generation').textContent=c.generation_ready?'Ready':'Blocked';$('credentialState').textContent=c.state||'Unknown';$('modelCount').textContent=models.length;$('persistence').textContent=p.restart_durable?'Durable':'Ephemeral';$('build').textContent=`Commit ${state.build?.render_commit||'unknown'} · contract ${state.build?.verification_contract||'unknown'}`;badge(c.generation_ready,c.generation_ready?'Connected':'Action required');kv($('runtimeKv'),{cookie_count:c.cookie_count,generation_ready:c.generation_ready,refresh_ready:c.refresh_ready,last_refresh_outcome:c.last_refresh_outcome,recovery_action:c.recovery_action});$('readiness').innerHTML=`<p class="${ready.ready?'':'notice'}">${ready.ready?'Ready for hosted generation':esc((ready.warnings||[]).join(' · ')||'Not ready')}</p>`;kv($('credentialKv'),{state:c.state,access_expires_in_seconds:c.access_expires_in_seconds,refresh_available:c.refresh_available,refresh_capture_state:c.refresh_capture_state,persistence_source:p.source,restart_durable:p.restart_durable});$('modelsBody').innerHTML=models.map(m=>`<tr><td>${esc(m.id)}</td><td>${esc(m.display_name||m.name||m.id)}</td><td>${esc(m.source||state.models?.source||'unknown')}</td><td>${esc(m.reasoning||m.metadata?.reasoning||'provider-defined')}</td></tr>`).join('')||'<tr><td colspan="4">No models reported</td></tr>';const features=state.capabilities?.features||[];$('capabilitiesBody').innerHTML=features.map(f=>`<tr><td>${esc(f.feature)}</td><td><span class="state ${esc(f.state)}">${esc(f.state)}</span></td><td>${esc(f.comparison)}</td><td>${esc(f.evidence_id)}</td></tr>`).join('');const v=state.verification?.verification||{};kv($('verificationKv'),{state:v.state,tested_commit:v.tested_commit,digest:v.digest,completed_at:v.completed_at?new Date(v.completed_at*1000).toLocaleString():null});const m=state.metrics||{};kv($('metricsKv'),{events:m.event_count,completed_generations:m.completed_generations,success_rate:m.success_rate,p50_ms:m.latency_ms?.p50,p95_ms:m.latency_ms?.p95,last_event_at:m.last_event_at?new Date(m.last_event_at*1000).toLocaleString():null});startLogs()}
function startLogs(){if(events)events.close();events=new EventSource('/dashboard/api/logs/stream');events.addEventListener('telemetry',event=>{let item={};try{item=JSON.parse(event.data)}catch{};const row=document.createElement('div');row.className='log';row.textContent=`${new Date((item.timestamp||0)*1000).toLocaleTimeString()}  ${item.event||'event'}  ${item.status||''}  ${item.model||''}  ${item.error_phase||''}`;$('logBox').appendChild(row);$('logBox').scrollTop=$('logBox').scrollHeight;while($('logBox').children.length>200)$('logBox').firstChild.remove()})}
$('loginButton').onclick=async()=>{const key=$('loginKey').value;$('loginError').textContent='';try{await api('/dashboard/login',{method:'POST',body:JSON.stringify({admin_key:key})});$('loginKey').value='';await load()}catch(error){$('loginKey').value='';$('loginError').textContent=error.message}};
$('logout').onclick=async()=>{await api('/dashboard/logout',{method:'POST'}).catch(()=>{});showLogin()};
document.querySelectorAll('.nav button').forEach(button=>button.onclick=()=>{document.querySelectorAll('.nav button').forEach(x=>x.classList.toggle('active',x===button));document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id===button.dataset.view));$('pageTitle').textContent=button.textContent});
$('openMicrosoft').onclick=()=>window.open('https://m365.cloud.microsoft/chat?auth=2','_blank','noopener,noreferrer');
$('credentialFile').onchange=async event=>{const file=event.target.files[0];if(file)$('credentialJson').value=await file.text();event.target.value=''};
$('importCredential').onclick=async()=>{let credential;try{credential=JSON.parse($('credentialJson').value)}catch{$('importStatus').textContent='Select or paste a valid OAuth JSON object.';return}$('importStatus').textContent='Importing…';try{const result=await api('/dashboard/api/credentials/import',{method:'POST',body:JSON.stringify({credential})});$('importStatus').textContent=`Imported: ${result.credential.state}`;await load()}catch(error){$('importStatus').textContent=error.message}finally{$('credentialJson').value='';credential=null}};
$('refresh').onclick=async()=>{$('actionStatus').textContent='Refreshing…';try{const result=await api('/dashboard/api/refresh',{method:'POST'});$('actionStatus').textContent=`Refresh: ${result.credential.state}`;await load()}catch(error){$('actionStatus').textContent=error.message}};
$('probe').onclick=async()=>{$('actionStatus').textContent='Running zero-cookie marker probe…';try{const result=await api('/dashboard/api/probe',{method:'POST'});$('actionStatus').textContent=`${result.state}; ${result.latency_ms} ms; marker ${result.marker_observed?'verified':'missing'}`;await load()}catch(error){$('actionStatus').textContent=error.message}};
fetch('/dashboard/api/overview').then(r=>{if(r.ok)return load();showLogin()}).catch(showLogin);
</script></body></html>"""

