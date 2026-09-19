"use strict";

let csrf = "";
let models = [];
let selectedSkill = null;
let selectedJob = null;
let traceSocket = null;
let selectedTraceId = null;
const traceCards = new Map();
const titles = {overview:"Overview",trace:"Live Trace",sessions:"Conversations / Sessions",routing:"Routing",integrations:"Integrations",models:"Models / STT",voice:"TTS / Voice",skills:"Skills",tools:"Tools",usage:"Usage",system:"System",logs:"Logs",security:"Security / Credential Status",passkeys:"Passkeys / Authentication",actions:"Actions / Broker",capabilities:"Capabilities",codex:"Codex Jobs"};

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-Butters-CSRF", csrf);
  const response = await fetch(path, {...options, headers, credentials:"same-origin"});
  const contentType = response.headers.get("content-type") || "";
  const value = contentType.includes("json") ? await response.json() : await response.blob();
  if (!response.ok) throw new Error(value.message || `Request failed: ${response.status}`);
  return {value, response};
}

function pretty(value) { return JSON.stringify(value, null, 2); }
function cards(container, value) {
  container.replaceChildren();
  for (const [key, raw] of Object.entries(value)) {
    const card=document.createElement("article"); card.className="metric-card";
    const label=document.createElement("small"); label.textContent=key.replaceAll("_"," ");
    const item=document.createElement("strong"); item.textContent=typeof raw === "object" ? JSON.stringify(raw) : String(raw);
    card.append(label,item); container.append(card);
  }
}
function rows(container, values, describe) {
  container.replaceChildren();
  for (const value of values) {
    const row=document.createElement("article"); row.className="data-row";
    const content=document.createElement("div"); const title=document.createElement("h3"); const detail=document.createElement("p");
    const described=describe(value); title.textContent=described[0]; detail.textContent=described[1]; content.append(title,detail); row.append(content); container.append(row);
  }
}

document.querySelector("#admin-nav").addEventListener("click", event => {
  const button=event.target.closest("button[data-panel]"); if(!button)return;
  document.querySelectorAll("#admin-nav button").forEach(item=>item.classList.toggle("active",item===button));
  document.querySelectorAll(".admin-panel").forEach(panel=>panel.classList.toggle("active",panel.id===`panel-${button.dataset.panel}`));
  document.querySelector("#panel-title").textContent=titles[button.dataset.panel];
  refresh(button.dataset.panel);
});

async function initialize() {
  try {
    const session=await fetch("/api/session",{credentials:"same-origin"}); const data=await session.json();
    if(!session.ok)throw new Error(data.message); csrf=data.csrf_token;
    const {value}=await api("/api/admin/overview");
    cards(document.querySelector("#overview-grid"),value);
    document.querySelector("#admin-status").textContent=`Authorized · ${value.administrator}`;
    const modelData=(await api("/api/admin/models")).value; models=modelData.text.models;
    const select=document.querySelector("#route-model"); select.replaceChildren(...models.map(model=>new Option(model,model)));
    const output=document.querySelector("#route-output"); output.max=String(modelData.text.max_output_tokens); output.value=String(modelData.text.max_output_tokens);
    await refreshTraces();
  } catch(error) { document.querySelector("#admin-status").textContent=error.message || "Denied"; }
}

async function refresh(panel) {
  try {
    if(panel==="overview") cards(document.querySelector("#overview-grid"),(await api("/api/admin/overview")).value);
    if(panel==="trace") {await refreshTraces();connectTraceSocket();}
    if(panel==="sessions") rows(document.querySelector("#session-list"),(await api("/api/admin/sessions")).value.sessions,item=>[item.session_id,`${item.message_count} messages · idle ${item.idle_seconds}s · ${item.context_chars} chars`]);
    if(panel==="models") renderObject(document.querySelector("#model-status"),(await api("/api/admin/models")).value);
    if(panel==="integrations") await refreshIntegrations();
    if(panel==="voice") await refreshVoice();
    if(panel==="skills") await refreshSkills();
    if(panel==="tools") {const data=(await api("/api/admin/tools")).value; rows(document.querySelector("#tool-list"),data.tools,item=>[item.name,`${item.action_class} · ${item.timeout_seconds}s · ${item.description}`]); await Promise.all([refreshDesktop(),refreshNas(),refreshPortalIdentities()]);}
    if(panel==="usage") renderObject(document.querySelector("#usage-view"),(await api("/api/admin/usage")).value);
    if(panel==="system") renderObject(document.querySelector("#system-view"),(await api("/api/admin/system")).value);
    if(panel==="logs") document.querySelector("#logs-view").textContent=pretty((await api("/api/admin/logs")).value);
    if(panel==="security") renderObject(document.querySelector("#security-view"),(await api("/api/admin/security")).value);
    if(panel==="passkeys") await refreshPasskeys();
    if(panel==="actions") renderObject(document.querySelector("#action-admin-view"),(await api("/api/admin/actions")).value);
    if(panel==="capabilities") renderCapabilities((await api("/api/capabilities")).value.capabilities);
    if(panel==="codex") await refreshJobs();
  } catch(error) { document.querySelector("#admin-status").textContent=error.message; }
}

function renderObject(container,value){rows(container,Object.entries(value),item=>[item[0].replaceAll("_"," "),typeof item[1]==="object"?pretty(item[1]):String(item[1])]);}

function decodeBase64url(value){const base64=value.replaceAll("-","+").replaceAll("_","/")+"=".repeat((4-value.length%4)%4);const binary=atob(base64);return Uint8Array.from(binary,character=>character.charCodeAt(0));}
function encodeBase64url(value){const bytes=new Uint8Array(value);let binary="";for(const byte of bytes)binary+=String.fromCharCode(byte);return btoa(binary).replaceAll("+","-").replaceAll("/","_").replaceAll("=","");}
function authOptions(value){const options={...value,challenge:decodeBase64url(value.challenge)};if(Array.isArray(value.allowCredentials))options.allowCredentials=value.allowCredentials.map(item=>({...item,id:decodeBase64url(item.id)}));return options;}
function registrationOptions(value){const options={...value,challenge:decodeBase64url(value.challenge),user:{...value.user,id:decodeBase64url(value.user.id)}};if(Array.isArray(value.excludeCredentials))options.excludeCredentials=value.excludeCredentials.map(item=>({...item,id:decodeBase64url(item.id)}));return options;}
function assertionJson(credential){return{id:credential.id,rawId:encodeBase64url(credential.rawId),type:credential.type,authenticatorAttachment:credential.authenticatorAttachment||null,clientExtensionResults:credential.getClientExtensionResults(),response:{authenticatorData:encodeBase64url(credential.response.authenticatorData),clientDataJSON:encodeBase64url(credential.response.clientDataJSON),signature:encodeBase64url(credential.response.signature),userHandle:credential.response.userHandle?encodeBase64url(credential.response.userHandle):null}};}
function registrationJson(credential){const transports=typeof credential.response.getTransports==="function"?credential.response.getTransports():[];return{id:credential.id,rawId:encodeBase64url(credential.rawId),type:credential.type,authenticatorAttachment:credential.authenticatorAttachment||null,clientExtensionResults:credential.getClientExtensionResults(),response:{attestationObject:encodeBase64url(credential.response.attestationObject),clientDataJSON:encodeBase64url(credential.response.clientDataJSON),transports}};}
async function authenticatePurpose(purpose="elevation",subject=null,pendingActionId=null){if(!window.PublicKeyCredential||!navigator.credentials)throw new Error("Passkeys are unavailable in this browser");const body={purpose};if(subject)body.subject=subject;if(pendingActionId)body.pending_action_id=pendingActionId;const begin=(await api("/api/auth/authenticate/options",{method:"POST",body:JSON.stringify(body)})).value;const credential=await navigator.credentials.get({publicKey:authOptions(begin.publicKey)});if(!credential)throw new Error("Authentication cancelled");return(await api("/api/auth/authenticate/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:assertionJson(credential)})})).value;}
function authStatusText(status){return status.elevated?`Elevated for ${status.remaining_seconds}s (server authoritative, non-sliding)`:`Locked · ${status.passkey_count} active passkey(s)`;}
async function refreshPasskeys(){const status=(await api("/api/auth/status")).value;document.querySelector("#auth-admin-status").textContent=authStatusText(status);const credentials=(await api("/api/auth/passkeys")).value.credentials;const container=document.querySelector("#passkey-list");container.replaceChildren();for(const credential of credentials){const row=document.createElement("article");row.className="data-row";const content=document.createElement("div");const title=document.createElement("h3");title.textContent=credential.label;const detail=document.createElement("p");detail.textContent=`Created ${new Date(credential.created_at*1000).toLocaleString()} · last used ${credential.last_used_at?new Date(credential.last_used_at*1000).toLocaleString():"never"}${credential.revoked?" · revoked":""}`;content.append(title,detail);row.append(content);if(!credential.revoked){const revoke=document.createElement("button");revoke.className="secondary-button";revoke.textContent="Revoke";revoke.addEventListener("click",()=>revokePasskey(credential.record_id));row.append(revoke);}container.append(row);}}
async function revokePasskey(recordId){if(!confirm("Revoke this passkey after a fresh assertion?"))return;try{const outcome=await authenticatePurpose("revoke_passkey",recordId);await api("/api/auth/passkeys/revoke",{method:"POST",body:JSON.stringify({record_id:recordId,fresh_grant:outcome.fresh_grant})});await refreshPasskeys();}catch(error){alert(error.message);}}
async function addPasskey(){try{const status=(await api("/api/auth/status")).value;const label=prompt("Passkey label","iPhone passkey");if(!label)return;let bootstrapToken=null;let freshGrant=null;if(status.passkey_count===0){bootstrapToken=prompt("Enter the short-lived token generated locally on the Pi");if(!bootstrapToken)return;}else{freshGrant=(await authenticatePurpose("register_passkey")).fresh_grant;}const begin=(await api("/api/auth/passkeys/register/options",{method:"POST",body:JSON.stringify({label,bootstrap_token:bootstrapToken,fresh_grant:freshGrant})})).value;const credential=await navigator.credentials.create({publicKey:registrationOptions(begin.publicKey)});if(!credential)throw new Error("Registration cancelled");await api("/api/auth/passkeys/register/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:registrationJson(credential)})});await refreshPasskeys();}catch(error){alert(error.message);}}
function renderCapabilities(values){rows(document.querySelector("#capability-list"),values,item=>[item.name,`${item.action_class} · auth ${item.authentication} · ${item.available?"available":item.unavailable_reason||"unavailable"}`]);}
document.querySelector("#admin-authenticate").addEventListener("click",async()=>{try{await authenticatePurpose();await refreshPasskeys();}catch(error){alert(error.message);}});
document.querySelector("#admin-lock").addEventListener("click",async()=>{try{await api("/api/auth/lock",{method:"POST"});await refreshPasskeys();}catch(error){alert(error.message);}});
document.querySelector("#add-passkey").addEventListener("click",addPasskey);

/* Poll one frozen action job to a terminal state. The job ID comes from the
 * server's own response to the POST that created it; nothing here can name an
 * arbitrary job. */
async function waitForAdminAction(jobId){for(let attempt=0;attempt<60;attempt+=1){const job=(await api(`/api/actions/jobs/${encodeURIComponent(jobId)}`)).value;if(job.state==="completed")return job;if(["failed","cancelled","expired"].includes(job.state))throw new Error(job.failure_reason||"the action did not complete");await new Promise(resolve=>window.setTimeout(resolve,500));}throw new Error("the action is still running; check Actions / Broker for its final status");}

async function refreshTraces(){renderTraces((await api("/api/admin/traces?limit=50")).value.traces);}
function createTraceCard(traceId){const card=document.createElement("details");card.className="trace-card";card.dataset.traceId=traceId;const summary=document.createElement("summary");const label=document.createElement("span");label.className="trace-summary-label";summary.append(label);summary.addEventListener("click",event=>{event.preventDefault();selectTrace(traceId);});const events=document.createElement("div");events.className="trace-events";card.append(summary,events);traceCards.set(traceId,card);return card;}
function selectTrace(traceId){const previous=selectedTraceId;selectedTraceId=previous===traceId?null:traceId;if(previous&&traceCards.has(previous))traceCards.get(previous).open=false;if(selectedTraceId&&traceCards.has(selectedTraceId))traceCards.get(selectedTraceId).open=true;}
function updateTraceCard(card,trace){card.querySelector(".trace-summary-label").textContent=`${trace.source} · ${trace.trace_id} · ${trace.completed?"complete":"live"}`;const events=card.querySelector(".trace-events");events.replaceChildren();for(const event of trace.events){const row=document.createElement("div");row.className="trace-event";const elapsed=document.createElement("code");elapsed.textContent=`${event.elapsed_ms}ms`;const stage=document.createElement("strong");stage.textContent=event.stage;const detail=document.createElement("span");detail.textContent=`${event.status}${event.reason_code?` · ${event.reason_code}`:""} ${JSON.stringify(event.fields)}`;row.append(elapsed,stage,detail);events.append(row);}const close=document.createElement("button");close.className="secondary-button trace-close";close.type="button";close.textContent="Close detail";close.addEventListener("click",event=>{event.stopPropagation();if(selectedTraceId===trace.trace_id)selectTrace(trace.trace_id);});events.append(close);card.open=selectedTraceId===trace.trace_id;}
function renderTraces(traces){const container=document.querySelector("#trace-list");const seen=new Set();for(const trace of traces){if(!trace||typeof trace.trace_id!=="string")continue;seen.add(trace.trace_id);const card=traceCards.get(trace.trace_id)||createTraceCard(trace.trace_id);updateTraceCard(card,trace);container.append(card);}for(const [traceId,card] of traceCards){if(!seen.has(traceId)&&traceId!==selectedTraceId){card.remove();traceCards.delete(traceId);}}}
function connectTraceSocket(){if(traceSocket&&[WebSocket.OPEN,WebSocket.CONNECTING].includes(traceSocket.readyState))return;const scheme=location.protocol==="https:"?"wss":"ws";traceSocket=new WebSocket(`${scheme}://${location.host}/ws/admin/traces`);traceSocket.onmessage=event=>{const data=JSON.parse(event.data);if(data.type==="traces")renderTraces(data.traces);};traceSocket.onclose=()=>{traceSocket=null;};}
document.querySelector("#refresh-traces").addEventListener("click",refreshTraces);
document.addEventListener("click",event=>{if(selectedTraceId&&!event.target.closest(".trace-card"))selectTrace(selectedTraceId);});

document.querySelector("#stt-test").addEventListener("click",async()=>{const output=document.querySelector("#stt-result");const file=document.querySelector("#stt-file").files[0];if(!file){output.textContent="Select a WAV file.";return;}if(file.size>8*1024*1024){output.textContent="WAV exceeds 8 MiB.";return;}output.textContent="Transcribing…";try{const data=(await api("/api/admin/stt/test",{method:"POST",headers:{"Content-Type":"audio/wav"},body:await file.arrayBuffer()})).value;output.textContent=pretty(data);}catch(error){output.textContent=error.message;}});

document.querySelector("#routing-form").addEventListener("submit",async event=>{event.preventDefault();const output=document.querySelector("#routing-result");output.textContent="Running…";try{const override=document.querySelector("#route-override").value;const body={text:document.querySelector("#routing-text").value,override,reasoning_effort:document.querySelector("#route-effort").value,max_output_tokens:Number(document.querySelector("#route-output").value)};if(override==="force_cloud_model")body.model=document.querySelector("#route-model").value;const data=(await api("/api/admin/routing/test",{method:"POST",body:JSON.stringify(body)})).value;output.textContent=pretty(data);}catch(error){output.textContent=error.message;}});

async function refreshVoice(){await refreshAiSettings();const data=(await api("/api/admin/voice/presets")).value;rows(document.querySelector("#voice-presets"),data.presets,item=>[item.name,`${item.provider} · ${item.model} · ${item.voice} · ${item.speed}x`]);}

async function refreshSkills(){const query=encodeURIComponent(document.querySelector("#skill-search").value);const data=(await api(`/api/admin/skills?q=${query}`)).value;const container=document.querySelector("#skill-list");container.replaceChildren();for(const skill of data.skills){const row=document.createElement("button");row.className="data-row";row.type="button";const text=document.createElement("div");const title=document.createElement("h3");title.textContent=skill.name;const detail=document.createElement("p");detail.textContent=`${skill.category} · ${skill.action_class} · ${skill.enabled?"enabled":"disabled"}`;text.append(title,detail);row.append(text);row.addEventListener("click",()=>selectSkill(skill));container.append(row);}}
function selectSkill(skill){selectedSkill=skill;document.querySelector("#skill-detail").textContent=pretty(skill);const test=document.querySelector("#skill-test");const toggle=document.querySelector("#skill-toggle");test.disabled=false;toggle.disabled=false;toggle.textContent=skill.enabled?"Disable":"Enable";}
document.querySelector("#skill-test").addEventListener("click",async()=>{if(!selectedSkill)return;const output=document.querySelector("#skill-test-result");try{const argumentsValue=JSON.parse(document.querySelector("#skill-test-args").value);if(!argumentsValue||Array.isArray(argumentsValue)||typeof argumentsValue!=="object")throw new Error("Arguments must be a JSON object");output.textContent=pretty((await api("/api/admin/skills/test",{method:"POST",body:JSON.stringify({name:selectedSkill.name,arguments:argumentsValue})})).value);}catch(error){output.textContent=error.message;}});
document.querySelector("#skill-toggle").addEventListener("click",async()=>{if(!selectedSkill)return;try{await api("/api/admin/skills/toggle",{method:"POST",body:JSON.stringify({name:selectedSkill.name,enabled:!selectedSkill.enabled})});await refreshSkills();selectedSkill=null;document.querySelector("#skill-detail").textContent="Select a skill.";document.querySelector("#skill-test").disabled=true;document.querySelector("#skill-toggle").disabled=true;}catch(error){document.querySelector("#skill-test-result").textContent=error.message;}});
document.querySelector("#skill-search").addEventListener("input",()=>{clearTimeout(window.skillTimer);window.skillTimer=setTimeout(refreshSkills,180);});
document.querySelector("#create-skill-shortcut").addEventListener("click",()=>{document.querySelector('[data-panel="codex"]').click();document.querySelector("#codex-description").focus();});

document.querySelector("#codex-form").addEventListener("submit",async event=>{event.preventDefault();const output=document.querySelector("#codex-result");output.textContent="Validating…";try{const data=(await api("/api/admin/codex/jobs",{method:"POST",body:JSON.stringify({description:document.querySelector("#codex-description").value})})).value;output.textContent=pretty(data);await refreshJobs();}catch(error){output.textContent=error.message;}});
async function refreshJobs(){const data=(await api("/api/admin/codex/jobs")).value;const container=document.querySelector("#codex-jobs");container.replaceChildren();for(const job of data.jobs){const row=document.createElement("button");row.className="data-row";row.type="button";const text=document.createElement("div");const title=document.createElement("h3");title.textContent=`${job.job_id} · ${job.status}`;const detail=document.createElement("p");detail.textContent=`${job.base_commit.slice(0,12)} · ${job.files_changed.length} files · ${job.stopping_reason||"pending"}`;text.append(title,detail);row.append(text);row.addEventListener("click",()=>selectJob(job.job_id));container.append(row);}}
async function selectJob(jobId){try{selectedJob=(await api(`/api/admin/codex/jobs/${encodeURIComponent(jobId)}`)).value;document.querySelector("#codex-job-detail").textContent=pretty(selectedJob);document.querySelector("#codex-run").disabled=!['queued','manual_launch_required'].includes(selectedJob.status);document.querySelector("#codex-approve").disabled=selectedJob.status!=="patch_ready"||!selectedJob.tests_passed;document.querySelector("#codex-reject").disabled=['approved_applied','rejected'].includes(selectedJob.status);}catch(error){document.querySelector("#codex-job-detail").textContent=error.message;}}
document.querySelector("#codex-run").addEventListener("click",async()=>{if(!selectedJob)return;try{selectedJob=(await api(`/api/admin/codex/jobs/${encodeURIComponent(selectedJob.job_id)}/run`,{method:"POST"})).value;document.querySelector("#codex-job-detail").textContent=pretty(selectedJob);await refreshJobs();}catch(error){document.querySelector("#codex-job-detail").textContent=error.message;}});
async function decideJob(decision){if(!selectedJob)return;if(decision==="approve"&&!confirm("Apply this reviewed patch to the clean repository worktree? This does not deploy or restart Butters."))return;try{selectedJob=(await api(`/api/admin/codex/jobs/${encodeURIComponent(selectedJob.job_id)}/decision`,{method:"POST",body:JSON.stringify({decision})})).value;document.querySelector("#codex-job-detail").textContent=pretty(selectedJob);await refreshJobs();}catch(error){document.querySelector("#codex-job-detail").textContent=error.message;}}
document.querySelector("#codex-approve").addEventListener("click",()=>decideJob("approve"));
document.querySelector("#codex-reject").addEventListener("click",()=>decideJob("reject"));

initialize();

/* ===================== Tools: Desktop and NAS =====================
 *
 * Two rules hold throughout this section.
 *
 * 1. CURRENT OBSERVED STATE and LAST OPERATION are rendered from separate
 *    server fields into separate places, and one never overwrites the other.
 *    A wake requested a minute ago cannot relabel a connected agent as
 *    offline, because the agent line is only ever written from `observed`.
 * 2. Every effectful control posts to one fixed endpoint that names one
 *    registered action server-side. No action name, host, or command is ever
 *    sent from this file.
 */

const AXIS_TONE = {yes:"good", present:"good", connected:"good", reachable:"good", ready:"good", no:"bad", absent:"bad", unreachable:"bad", unavailable:"bad", not_configured:"bad", unknown:"muted", not_observed:"muted", starting:"warn", heartbeat_aging:"warn", heartbeat_stale:"warn"};
const AXIS_TEXT = {yes:"Yes", no:"No", unknown:"Unknown", present:"Active", absent:"Not active", not_observed:"Not observed", reachable:"Reachable", unreachable:"Unreachable", ready:"Ready", starting:"Starting", unavailable:"Unavailable", connected:"Connected", not_configured:"Not configured", heartbeat_aging:"Heartbeat aging", heartbeat_stale:"Heartbeat stale", disconnected:"Disconnected"};

function axis(container, entries) {
  container.replaceChildren();
  for (const [label, raw, note] of entries) {
    const cell=document.createElement("article"); cell.className=`axis-cell axis-${AXIS_TONE[raw]||"muted"}`;
    const name=document.createElement("small"); name.textContent=label;
    const value=document.createElement("strong"); value.textContent=AXIS_TEXT[raw]||String(raw);
    cell.append(name,value);
    if(note){const detail=document.createElement("span"); detail.className="axis-note"; detail.textContent=note; cell.append(detail);}
    container.append(cell);
  }
}

function ago(seconds){ if(seconds===null||seconds===undefined)return "unknown"; const value=Math.round(seconds); if(value<60)return `${value} second${value===1?"":"s"} ago`; const minutes=Math.round(value/60); if(minutes<60)return `${minutes} minute${minutes===1?"":"s"} ago`; const hours=Math.round(minutes/60); return `${hours} hour${hours===1?"":"s"} ago`; }

function renderLastOperation(node, record, empty) {
  // Purely historical. Never feeds any observed-state line.
  if(!record){ node.textContent=empty; node.classList.remove("stale-operation"); return; }
  node.textContent=`${record.operation} · ${record.outcome}${record.detail?` (${record.detail})`:""} · ${ago(record.age_seconds)}`;
  node.classList.toggle("stale-operation", (record.age_seconds||0) > 300);
}

/* ---------- Desktop ---------- */

async function refreshDesktop() {
  const summary=document.querySelector("#desktop-summary");
  try {
    const state=(await api("/api/admin/tools/desktop")).value;
    const observed=state.observed;
    summary.textContent=state.summary;
    axis(document.querySelector("#desktop-status"), [
      ["Power / network", observed.power_network],
      ["SSH", observed.ssh],
      ["Parsec", observed.parsec],
      ["Desktop Agent", observed.agent],
      ["Windows session", observed.windows_session],
      ["Agent heartbeat", observed.agent_heartbeat_age_seconds===null?"unknown":"yes", observed.agent_heartbeat_age_seconds===null?undefined:`${Math.round(observed.agent_heartbeat_age_seconds)}s ago`],
    ]);
    // Written only from `observed`, so a stale workflow cannot contradict it.
    document.querySelector("#desktop-agent-status").textContent=
      observed.agent_connected
        ? `Agent connected · Windows session ${AXIS_TEXT[observed.windows_session]||observed.windows_session} · heartbeat ${observed.agent_heartbeat_age_seconds===null?"unknown":`${Math.round(observed.agent_heartbeat_age_seconds)}s ago`}`
        : `Agent ${AXIS_TEXT[observed.agent]||observed.agent}${state.agent_detail.reason?` · ${state.agent_detail.reason}`:""}`;
    renderLastOperation(document.querySelector("#desktop-last-operation"), state.last_operation, "No desktop operation has been requested from this console.");
    renderDesktopApps(state.apps);
    document.querySelector("#desktop-vms").textContent=`${state.vm.headline}. ${state.vm.detail}`;
    document.querySelector("#desktop-shutdown").disabled=!state.configured.shutdown;
    document.querySelector("#desktop-streaming").disabled=!state.configured.streaming;
    document.querySelector("#desktop-wake").disabled=!state.configured.wake;
  } catch(error) { summary.textContent=`Desktop status unavailable: ${error.message||"unknown error"}`; }
}

function renderDesktopApps(apps) {
  const grid=document.querySelector("#desktop-apps"); grid.replaceChildren();
  if(!apps.observed){
    const note=document.createElement("p"); note.className="tool-note";
    note.textContent=apps.reason==="agent_not_connected"
      ? "Application controls appear when the Desktop Agent is connected."
      : `Application catalog unavailable: ${apps.reason}.`;
    grid.append(note); return;
  }
  if(!apps.apps.length){
    const note=document.createElement("p"); note.className="tool-note";
    note.textContent="The Desktop Agent reported an empty allowlist."; grid.append(note); return;
  }
  for(const app of apps.apps){
    const button=document.createElement("button"); button.className="secondary-button app-button";
    button.type="button"; button.dataset.app=app.app;
    button.textContent=app.display_name||app.app;
    if(app.installed===false){button.disabled=true; button.title="Not installed on the desktop";}
    button.addEventListener("click",()=>launchDesktopApp(app.app,button.textContent));
    grid.append(button);
  }
}

async function launchDesktopApp(app, label) {
  await runDesktopAction(`Launch ${label}`, () => api("/api/admin/tools/desktop/launch-app",{method:"POST",body:JSON.stringify({app})}));
}

async function runDesktopAction(label, request) {
  const summary=document.querySelector("#desktop-result-summary");
  const detail=document.querySelector("#desktop-result");
  summary.textContent=`${label}: submitting…`;
  try {
    let result=(await request()).value;
    if(result.status==="authentication_required"){
      summary.textContent=`${label}: passkey authentication required…`;
      result=await authenticatePurpose("pending_action",null,result.pending_action.pending_action_id);
    }
    if(!Array.isArray(result.jobs)||!result.jobs.length) throw new Error("the action was not queued");
    const job=await waitForAdminAction(result.jobs[0].job_id);
    summary.textContent=`${label}: completed.`;
    detail.textContent=pretty(job);
  } catch(error) {
    summary.textContent=`${label} failed: ${error.message||"unknown error"}`;
    detail.textContent=String(error.message||error);
  }
  await refreshDesktop();
}

/* ---------- NAS ---------- */

async function refreshNas() {
  const summary=document.querySelector("#nas-summary");
  try {
    const state=(await api("/api/admin/tools/nas?refresh=1")).value;
    const observations=state.observations;
    summary.textContent=`${state.aggregate}`;
    axis(document.querySelector("#nas-status"), [
      ["LAN reachability", observations.lan],
      ["NAS OS / API", observations.nas_api],
      ["Tailscale", observations.tailscale],
      ["Jellyfin", observations.jellyfin],
    ]);
    renderLastOperation(document.querySelector("#nas-last-operation"), state.last_operation, "No NAS operation has been requested from this console.");
    document.querySelector("#nas-wake").disabled=!state.capability.wake_configured;
    document.querySelector("#nas-shutdown").disabled=!state.capability.shutdown_configured;
    if(!state.capability.shutdown_configured){
      document.querySelector("#nas-shutdown-status").textContent="NAS shutdown is not enabled in configuration or at the broker gate.";
    }
  } catch(error) { summary.textContent=`NAS status unavailable: ${error.message||"unknown error"}`; }
}

async function wakeNas() {
  const button=document.querySelector("#nas-wake"); const status=document.querySelector("#nas-action-status");
  button.disabled=true; status.textContent="Submitting the fixed NAS wake action…";
  try {
    let result=(await api("/api/admin/tools/wake-nas",{method:"POST",body:JSON.stringify({})})).value;
    if(result.status==="authentication_required"){
      status.textContent="Passkey authentication required…";
      result=await authenticatePurpose("pending_action",null,result.pending_action.pending_action_id);
    }
    if(!Array.isArray(result.jobs)||!result.jobs.length) throw new Error("NAS wake was not queued");
    await waitForAdminAction(result.jobs[0].job_id);
    // Deliberate wording: one packet left this host. Boot is decided by polling.
    status.textContent="Wake packet sent. Polling current state to see whether the NAS boots…";
  } catch(error) { status.textContent=`Wake NAS failed: ${error.message||"unknown error"}`; }
  finally { button.disabled=false; await refreshNas(); }
}

function confirmPanel(node, message, onConfirm) {
  node.replaceChildren();
  const text=document.createElement("p"); text.textContent=message;
  const row=document.createElement("div"); row.className="button-row";
  const yes=document.createElement("button"); yes.className="danger-button"; yes.type="button"; yes.textContent="Confirm";
  const no=document.createElement("button"); no.className="secondary-button"; no.type="button"; no.textContent="Cancel";
  no.addEventListener("click",()=>{node.hidden=true;});
  yes.addEventListener("click",()=>{node.hidden=true; onConfirm();});
  row.append(yes,no); node.append(text,row); node.hidden=false;
}

async function shutdownNas() {
  const status=document.querySelector("#nas-shutdown-status");
  status.textContent="Submitting the fixed NAS shutdown action…";
  try {
    let result=(await api("/api/admin/tools/shutdown-nas",{method:"POST",body:JSON.stringify({confirm:true})})).value;
    if(result.status==="authentication_required"){
      status.textContent="Fresh passkey authentication bound to this exact action is required…";
      result=await authenticatePurpose("pending_action",null,result.pending_action.pending_action_id);
    }
    if(!Array.isArray(result.jobs)||!result.jobs.length) throw new Error("NAS shutdown was not queued");
    await waitForAdminAction(result.jobs[0].job_id);
    status.textContent="Shutdown requested. Polling current state…";
  } catch(error) { status.textContent=`Shut Down NAS failed: ${error.message||"unknown error"}`; }
  finally { await refreshNas(); }
}

/* ---------- Portal enrollment ---------- */

async function refreshPortalIdentities() {
  try {
    const data=(await api("/api/admin/portal/identities")).value;
    const container=document.querySelector("#portal-identity-list"); container.replaceChildren();
    for(const item of data.identities){
      const row=document.createElement("article"); row.className="data-row";
      const content=document.createElement("div");
      const title=document.createElement("h3"); title.textContent=`${item.label} · ${item.identity}`;
      const detail=document.createElement("p"); detail.textContent=`${item.roles.join(", ")}${item.revoked?" · revoked":""}`;
      content.append(title,detail); row.append(content);
      if(!item.revoked){
        const revoke=document.createElement("button"); revoke.className="secondary-button"; revoke.textContent="Revoke access";
        revoke.addEventListener("click",()=>revokePortalIdentity(item.identity));
        row.append(revoke);
      }
      container.append(row);
    }
    for(const invite of data.pending_invites){
      const row=document.createElement("article"); row.className="data-row";
      const content=document.createElement("div");
      const title=document.createElement("h3"); title.textContent=`Pending invitation · ${invite.identity}`;
      const detail=document.createElement("p"); detail.textContent=`${invite.label} · expires ${new Date(invite.expires_at*1000).toLocaleString()}`;
      content.append(title,detail); row.append(content); container.append(row);
    }
  } catch(error) { document.querySelector("#portal-invite-status").textContent=error.message||"unknown error"; }
}

async function createPortalInvite() {
  const status=document.querySelector("#portal-invite-status");
  const identity=document.querySelector("#portal-identity").value.trim();
  const label=document.querySelector("#portal-label").value.trim();
  if(!identity||!label){status.textContent="An identity and a label are both required.";return;}
  try {
    const result=(await api("/api/admin/portal/invite",{method:"POST",body:JSON.stringify({identity,label})})).value;
    // Shown once, in the administrator's own browser, and never stored here.
    status.textContent=`Invitation for ${result.identity}: ${result.invite_token} — give this to them over a channel you trust. It is single-use and expires ${new Date(result.expires_at*1000).toLocaleString()}.`;
    await refreshPortalIdentities();
  } catch(error) { status.textContent=`Invitation failed: ${error.message||"unknown error"}`; }
}

async function revokePortalIdentity(identity) {
  if(!confirm(`Revoke portal access for ${identity}?`))return;
  try { await api("/api/admin/portal/revoke",{method:"POST",body:JSON.stringify({identity})}); await refreshPortalIdentities(); }
  catch(error) { document.querySelector("#portal-invite-status").textContent=error.message||"unknown error"; }
}

/* ---------- wiring ---------- */

document.querySelector("#desktop-refresh").addEventListener("click",refreshDesktop);
document.querySelector("#desktop-ssh-test").addEventListener("click",async()=>{
  const summary=document.querySelector("#desktop-result-summary");
  summary.textContent="SSH Test: probing…";
  try {
    const result=(await api("/api/admin/tools/desktop/ssh-test",{method:"POST",body:JSON.stringify({})})).value;
    summary.textContent=`SSH Test: port ${AXIS_TEXT[result.ssh]||result.ssh} (TCP connect only — no session, no command).`;
    document.querySelector("#desktop-result").textContent=pretty(result);
  } catch(error) { summary.textContent=`SSH Test failed: ${error.message||"unknown error"}`; }
  await refreshDesktop();
});
document.querySelector("#desktop-wake").addEventListener("click",()=>runDesktopAction("Wake Desktop",()=>api("/api/admin/tools/desktop/wake",{method:"POST",body:JSON.stringify({})})));
document.querySelector("#desktop-streaming").addEventListener("click",()=>runDesktopAction("Prepare for Streaming",()=>api("/api/admin/tools/desktop/streaming",{method:"POST",body:JSON.stringify({})})));
document.querySelector("#desktop-shutdown").addEventListener("click",()=>{
  confirmPanel(document.querySelector("#desktop-shutdown-confirm"),
    "This ends every interactive desktop session, including Parsec, and any running build.",
    ()=>runDesktopAction("Shut Down Desktop",()=>api("/api/admin/tools/desktop/shutdown",{method:"POST",body:JSON.stringify({})})));
});
document.querySelector("#nas-refresh").addEventListener("click",refreshNas);
document.querySelector("#nas-wake").addEventListener("click",wakeNas);
document.querySelector("#nas-shutdown").addEventListener("click",()=>{
  confirmPanel(document.querySelector("#nas-shutdown-confirm"),
    "This powers the NAS off. Jellyfin and every share it serves stop until it is woken again.",
    shutdownNas);
});
document.querySelector("#portal-invite").addEventListener("click",createPortalInvite);


/* ================= Integrations, Chat model, and Text to speech ============
 *
 * Three rules hold throughout this section.
 *
 * 1. The backend catalog decides what exists. Every provider, model, voice,
 *    effort, and range below is read from /api/admin/ai/catalog. This file
 *    contains no model identifier, no voice name, and no numeric bound of its
 *    own, so it cannot drift away from what the server will accept.
 * 2. A control appears only when the selected model declares support for it.
 *    Hiding is a convenience; the server refuses an unsupported parameter
 *    regardless of what this page sends.
 * 3. The candidate API key exists only inside one form field and one POST
 *    body. It is never placed in a URL, never written to localStorage,
 *    sessionStorage, IndexedDB, or a cookie, never kept in a module variable,
 *    and is cleared from the field on success and on failure alike.
 */

let aiCatalog = null;
let aiState = null;

function option(value, label) { return new Option(label, value); }
function show(node, visible) { if(node) node.hidden = !visible; }

function providerById(list, id) { return list.find(item => item.id === id) || list[0] || null; }
function modelById(list, id) { return list.find(item => item.id === id) || list[0] || null; }

async function loadAiCatalog() {
  if(!aiCatalog) aiCatalog = (await api("/api/admin/ai/catalog")).value;
  return aiCatalog;
}

async function refreshAiSettings() {
  await loadAiCatalog();
  aiState = (await api("/api/admin/ai/settings")).value;
  renderChatForm();
  renderTtsForm();
  renderEffectiveHeadlines();
}

function renderEffectiveHeadlines() {
  const chat = aiState.effective.chat;
  const speech = aiState.effective.speech;
  const chatNode = document.querySelector("#chat-effective");
  const ttsNode = document.querySelector("#tts-effective");
  // SAVED and EFFECTIVE are separate server fields and are reported
  // separately. A saved row is never announced as a running configuration.
  if(chatNode) chatNode.textContent = aiState.in_sync.chat
    ? `Effective: ${chat.provider} · ${chat.model}${chat.reasoning_effort?` · ${chat.reasoning_effort}`:""}`
    : `Saved configuration is not active. Still running ${chat.provider} · ${chat.model}.`;
  if(ttsNode) ttsNode.textContent = aiState.in_sync.speech
    ? `Effective: ${speech.provider} · ${speech.model} · ${speech.voice}`
    : `Saved configuration is not active. Still speaking with ${speech.model} · ${speech.voice}.`;
  if(aiState.activation_error){
    const message = `Runtime activation failed: ${aiState.activation_error.message}`;
    if(chatNode && !aiState.in_sync.chat) chatNode.textContent = message;
    if(ttsNode && !aiState.in_sync.speech) ttsNode.textContent = message;
  }
}

/* ---------- Butters Chat model ---------- */

function renderChatForm() {
  const providers = aiCatalog.chat_providers;
  const saved = aiState.saved.chat;
  const providerSelect = document.querySelector("#chat-provider");
  if(!providerSelect) return;
  providerSelect.replaceChildren(...providers.map(item => option(item.id, item.label)));
  providerSelect.value = providerById(providers, saved.provider).id;
  renderChatModels(saved);
}

function renderChatModels(saved) {
  const providers = aiCatalog.chat_providers;
  const provider = providerById(providers, document.querySelector("#chat-provider").value);
  const modelSelect = document.querySelector("#chat-model");
  modelSelect.replaceChildren(...provider.chat_models.map(item =>
    option(item.id, item.note ? `${item.label} — ${item.note}` : item.label)));
  const chosen = provider.id === saved.provider ? saved.model : provider.chat_models[0].id;
  modelSelect.value = modelById(provider.chat_models, chosen).id;
  renderChatCapabilities(provider.id === saved.provider ? saved : null);
}

function renderChatCapabilities(saved) {
  const provider = providerById(aiCatalog.chat_providers, document.querySelector("#chat-provider").value);
  const model = modelById(provider.chat_models, document.querySelector("#chat-model").value);
  const supports = model.supports;

  const effort = document.querySelector("#chat-effort");
  effort.replaceChildren(option("", "Unset (provider default)"),
    ...model.reasoning_efforts.map(value => option(value, value)));
  effort.value = saved && saved.reasoning_effort ? saved.reasoning_effort : "";
  show(document.querySelector("#chat-effort-field"), supports.reasoning_effort);

  const verbosity = document.querySelector("#chat-verbosity");
  verbosity.replaceChildren(option("", "Unset (provider default)"),
    ...aiCatalog.verbosity_levels.map(value => option(value, value)));
  verbosity.value = saved && saved.verbosity ? saved.verbosity : "";
  show(document.querySelector("#chat-verbosity-field"), supports.verbosity);

  const truncation = document.querySelector("#chat-truncation");
  truncation.replaceChildren(option("", "Unset (provider default)"),
    ...aiCatalog.truncation_modes.map(value => option(value, value)));
  truncation.value = saved && saved.truncation ? saved.truncation : "";
  show(document.querySelector("#chat-truncation-field"), supports.truncation);

  const output = document.querySelector("#chat-output");
  output.max = String(aiCatalog.max_output_tokens);
  output.value = saved && saved.max_output_tokens ? String(saved.max_output_tokens) : "";

  const temperature = document.querySelector("#chat-temperature");
  temperature.value = saved && saved.temperature !== null && saved.temperature !== undefined ? String(saved.temperature) : "";
  show(document.querySelector("#chat-temperature-field"), supports.temperature);

  const topP = document.querySelector("#chat-top-p");
  topP.value = saved && saved.top_p !== null && saved.top_p !== undefined ? String(saved.top_p) : "";
  show(document.querySelector("#chat-top-p-field"), supports.top_p);

  const toolCalls = document.querySelector("#chat-tool-calls");
  toolCalls.value = saved && saved.max_tool_calls !== null && saved.max_tool_calls !== undefined ? String(saved.max_tool_calls) : "";
  show(document.querySelector("#chat-tool-calls-field"), supports.max_tool_calls);

  document.querySelector("#chat-parallel").checked = Boolean(saved && saved.parallel_tool_calls);
  show(document.querySelector("#chat-parallel-field"), supports.parallel_tool_calls);
  document.querySelector("#chat-store").checked = Boolean(saved && saved.store_responses);
  show(document.querySelector("#chat-store-field"), supports.store);
  document.querySelector("#chat-cache").checked = Boolean(saved && saved.prompt_cache_enabled);
  show(document.querySelector("#chat-cache-field"), supports.prompt_cache_key);
}

function numberOrNull(selector) {
  const raw = document.querySelector(selector).value.trim();
  return raw === "" ? null : Number(raw);
}
function textOrNull(selector) {
  const raw = document.querySelector(selector).value.trim();
  return raw === "" ? null : raw;
}
function checkedOrNull(selector, supported) {
  return supported ? document.querySelector(selector).checked : null;
}

function chatBody() {
  const provider = providerById(aiCatalog.chat_providers, document.querySelector("#chat-provider").value);
  const model = modelById(provider.chat_models, document.querySelector("#chat-model").value);
  const supports = model.supports;
  // An unset control is sent as null, which the server stores as unset and
  // omits from the provider request. It is never replaced with a default.
  return {
    provider: provider.id,
    model: model.id,
    reasoning_effort: supports.reasoning_effort ? textOrNull("#chat-effort") : null,
    verbosity: supports.verbosity ? textOrNull("#chat-verbosity") : null,
    max_output_tokens: numberOrNull("#chat-output"),
    temperature: supports.temperature ? numberOrNull("#chat-temperature") : null,
    top_p: supports.top_p ? numberOrNull("#chat-top-p") : null,
    truncation: supports.truncation ? textOrNull("#chat-truncation") : null,
    max_tool_calls: supports.max_tool_calls ? numberOrNull("#chat-tool-calls") : null,
    parallel_tool_calls: checkedOrNull("#chat-parallel", supports.parallel_tool_calls),
    store_responses: checkedOrNull("#chat-store", supports.store),
    prompt_cache_enabled: checkedOrNull("#chat-cache", supports.prompt_cache_key),
  };
}

/* ---------- Text to speech ---------- */

function renderTtsForm() {
  const providers = aiCatalog.speech_providers;
  const saved = aiState.saved.speech;
  const providerSelect = document.querySelector("#tts-provider");
  if(!providerSelect) return;
  providerSelect.replaceChildren(...providers.map(item => option(item.id, item.label)));
  providerSelect.value = providerById(providers, saved.provider).id;
  renderTtsModels(saved);
}

function renderTtsModels(saved) {
  const provider = providerById(aiCatalog.speech_providers, document.querySelector("#tts-provider").value);
  const modelSelect = document.querySelector("#tts-model");
  modelSelect.replaceChildren(...provider.speech_models.map(item => option(item.id, item.label)));
  const chosen = saved && provider.id === saved.provider ? saved.model : provider.speech_models[0].id;
  modelSelect.value = modelById(provider.speech_models, chosen).id;
  renderTtsCapabilities(saved && provider.id === saved.provider ? saved : null);
}

function renderTtsCapabilities(saved) {
  const provider = providerById(aiCatalog.speech_providers, document.querySelector("#tts-provider").value);
  const model = modelById(provider.speech_models, document.querySelector("#tts-model").value);
  document.querySelector("#tts-model-note").textContent = model.note || "";

  const voices = document.querySelector("#tts-voice");
  voices.replaceChildren(...model.voices.map(item => option(item.id, item.label)));
  const chosenVoice = saved && model.voices.some(item => item.id === saved.voice) ? saved.voice : model.voices[0].id;
  voices.value = chosenVoice;

  const speed = document.querySelector("#tts-speed");
  speed.min = String(model.speed.minimum);
  speed.max = String(model.speed.maximum);
  speed.step = String(model.speed.step);
  speed.value = String(saved && saved.speed !== null && saved.speed !== undefined ? saved.speed : 1);
  document.querySelector("#tts-speed-value").textContent = `${Number(speed.value).toFixed(2)}×`;
  show(document.querySelector("#tts-speed-field"), model.supports.speed);

  // The style box is not merely hidden for a model that ignores it: the value
  // is cleared, so a style typed for one model cannot survive a switch and be
  // saved against a model that would never receive it.
  const instructions = document.querySelector("#tts-instructions");
  instructions.value = model.supports.instructions && saved && saved.instructions ? saved.instructions : "";
  show(document.querySelector("#tts-instructions-field"), model.supports.instructions);
  document.querySelector("#tts-instructions-note").textContent = model.supports.instructions
    ? "Sent with every synthesis request for this model."
    : `${model.label} does not accept a speaking style, so none is sent.`;
  show(document.querySelector("#tts-instructions-note"), true);

  const formats = document.querySelector("#tts-format");
  formats.replaceChildren(...model.formats.map(value => option(value, value)));
  formats.value = saved && saved.audio_format ? saved.audio_format : model.formats[0];
}

function ttsBody() {
  const provider = providerById(aiCatalog.speech_providers, document.querySelector("#tts-provider").value);
  const model = modelById(provider.speech_models, document.querySelector("#tts-model").value);
  return {
    provider: provider.id,
    model: model.id,
    voice: document.querySelector("#tts-voice").value,
    speed: model.supports.speed ? Number(document.querySelector("#tts-speed").value) : null,
    instructions: model.supports.instructions ? textOrNull("#tts-instructions") : null,
    audio_format: document.querySelector("#tts-format").value || null,
  };
}

/* ---------- OpenAI credential ---------- */

async function refreshIntegrations() {
  await refreshAiSettings();
  renderCredential(aiState.credential);
}

function renderCredential(credential) {
  const summary = document.querySelector("#openai-summary");
  const validation = credential.last_validation;
  summary.textContent = credential.configured
    ? `Credential configured · ${credential.source === "butters_store" ? "stored in Butters" : "supplied by the service unit"}`
    : "No OpenAI credential is configured";
  axis(document.querySelector("#openai-credential"), [
    ["Credential", credential.configured ? "present" : "absent"],
    ["Validation", validation ? (validation.authenticated ? "yes" : "no") : "unknown",
      validation ? validation.detail : "never validated from this console"],
    ["Last validated", credential.last_validated_at ? "yes" : "unknown",
      credential.last_validated_at ? new Date(credential.last_validated_at * 1000).toLocaleString() : undefined],
    ["Model availability", validation && validation.model_available !== null && validation.model_available !== undefined
      ? (validation.model_available ? "yes" : "no") : "unknown",
      validation && validation.model_checked ? validation.model_checked : undefined],
    ["Identity", credential.fingerprint ? "present" : "absent", credential.fingerprint || undefined],
  ]);
  document.querySelector("#openai-test").disabled = !credential.configured;
  document.querySelector("#openai-remove").disabled = !credential.configured || credential.source !== "butters_store";
}

function clearKeyField() {
  const field = document.querySelector("#openai-key");
  field.value = "";
  // Defensive: some browsers keep the last value for an autofilled field.
  field.setAttribute("value", "");
}

async function submitCredential(event) {
  event.preventDefault();
  const status = document.querySelector("#openai-status");
  const field = document.querySelector("#openai-key");
  const candidate = field.value;
  if(!candidate){ status.textContent = "Enter an API key first."; return; }
  status.textContent = "Fresh passkey authentication bound to this change is required…";
  try {
    const grant = (await authenticatePurpose("openai_credential", "set")).fresh_grant;
    status.textContent = "Validating the candidate against OpenAI…";
    const result = (await api("/api/admin/integrations/openai/key", {
      method: "POST",
      body: JSON.stringify({api_key: candidate, fresh_grant: grant, confirm: true}),
    })).value;
    if(result.replaced){
      status.textContent = "Validated and stored. The provider was reinitialized with the new credential.";
    } else if(result.activation_error){
      status.textContent = `Validated, but activation failed (${result.activation_error.message}). The previous credential is still active.`;
    } else {
      status.textContent = `Validation failed (${result.validation.detail}). The existing credential is unchanged and the candidate was discarded.`;
    }
    renderCredential(result.credential);
  } catch(error) {
    // Never echo the candidate, not even in a failure message.
    status.textContent = `Setting the API key failed: ${error.message||"unknown error"}`;
  } finally {
    clearKeyField();
    document.querySelector("#openai-key-form").hidden = true;
    await refreshIntegrations();
  }
}

async function removeCredential() {
  const status = document.querySelector("#openai-status");
  status.textContent = "Fresh passkey authentication bound to this removal is required…";
  try {
    const grant = (await authenticatePurpose("openai_credential", "remove")).fresh_grant;
    const result = (await api("/api/admin/integrations/openai/key", {
      method: "DELETE",
      body: JSON.stringify({fresh_grant: grant, confirm: true}),
    })).value;
    status.textContent = `${result.removed ? "Removed from Butters." : "No stored credential to remove."} ${result.notice}`;
    renderCredential(result.credential);
  } catch(error) {
    status.textContent = `Removing the credential failed: ${error.message||"unknown error"}`;
  }
  await refreshIntegrations();
}

/* ---------- wiring ---------- */

document.querySelector("#chat-provider").addEventListener("change", () => renderChatModels(aiState.saved.chat));
document.querySelector("#chat-model").addEventListener("change", () => renderChatCapabilities(
  aiState.saved.chat.model === document.querySelector("#chat-model").value ? aiState.saved.chat : null));
document.querySelector("#chat-save").addEventListener("click", async () => {
  const status = document.querySelector("#chat-status");
  status.textContent = "Saving and applying…";
  try {
    aiState = (await api("/api/admin/ai/chat", {method:"POST", body: JSON.stringify(chatBody())})).value;
    status.textContent = aiState.in_sync.chat
      ? "Saved and active."
      : `Saved, but not active: ${aiState.activation_error ? aiState.activation_error.message : "runtime reload did not complete"}.`;
    renderEffectiveHeadlines();
  } catch(error) { status.textContent = `Save failed: ${error.message||"unknown error"}`; }
});

document.querySelector("#tts-provider").addEventListener("change", () => renderTtsModels(aiState.saved.speech));
document.querySelector("#tts-model").addEventListener("change", () => renderTtsCapabilities(
  aiState.saved.speech.model === document.querySelector("#tts-model").value ? aiState.saved.speech : null));
document.querySelector("#tts-speed").addEventListener("input", event => {
  document.querySelector("#tts-speed-value").textContent = `${Number(event.target.value).toFixed(2)}×`;
});
document.querySelector("#tts-save").addEventListener("click", async () => {
  const status = document.querySelector("#tts-status");
  status.textContent = "Saving and applying…";
  try {
    aiState = (await api("/api/admin/ai/tts", {method:"POST", body: JSON.stringify(ttsBody())})).value;
    status.textContent = aiState.in_sync.speech
      ? "Saved and active. The next spoken chat answer uses this voice."
      : `Saved, but not active: ${aiState.activation_error ? aiState.activation_error.message : "runtime reload did not complete"}.`;
    renderEffectiveHeadlines();
  } catch(error) { status.textContent = `Save failed: ${error.message||"unknown error"}`; }
});
document.querySelector("#tts-preview").addEventListener("click", async () => {
  const status = document.querySelector("#tts-status");
  status.textContent = "Synthesizing a preview through the Butters Chat speech path…";
  try {
    const body = {...ttsBody(), name: "preview", phrase: "Hello. This is Butters, checking the home sensor stack."};
    delete body.audio_format;
    if(body.instructions === null) delete body.instructions;
    if(body.speed === null) body.speed = 1;
    const {value} = await api("/api/admin/voice/preview", {method:"POST", body: JSON.stringify(body)});
    const audio = document.querySelector("#voice-audio");
    audio.src = URL.createObjectURL(value);
    await audio.play();
    status.textContent = "Preview generated with the same provider, model, voice, and speed a chat answer would use.";
  } catch(error) { status.textContent = `Preview failed: ${error.message||"unknown error"}`; }
});

document.querySelector("#openai-test").addEventListener("click", async () => {
  const status = document.querySelector("#openai-status");
  status.textContent = "Testing the stored credential…";
  try {
    const result = (await api("/api/admin/integrations/openai/test", {method:"POST", body: JSON.stringify({})})).value;
    const validation = result.validation;
    status.textContent = validation.authenticated
      ? `Credential authenticates. Model ${validation.model_checked}: ${validation.model_available ? "available to this project" : "not available to this project"}.`
      : `Credential did not authenticate: ${validation.detail}.`;
    renderCredential(result.credential);
  } catch(error) { status.textContent = `Credential test failed: ${error.message||"unknown error"}`; }
});
document.querySelector("#openai-set").addEventListener("click", () => {
  clearKeyField();
  document.querySelector("#openai-key-form").hidden = false;
  document.querySelector("#openai-key").focus();
});
document.querySelector("#openai-key-cancel").addEventListener("click", () => {
  clearKeyField();
  document.querySelector("#openai-key-form").hidden = true;
});
document.querySelector("#openai-key-form").addEventListener("submit", submitCredential);
document.querySelector("#openai-remove").addEventListener("click", () => {
  confirmPanel(document.querySelector("#openai-remove-confirm"),
    "This deletes Butters' local copy of the OpenAI credential. Cloud chat and cloud speech stop until a new key is stored. It does not revoke anything in your OpenAI account.",
    removeCredential);
});
