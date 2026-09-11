"use strict";

let csrf = "";
let models = [];
let selectedSkill = null;
let selectedJob = null;
let traceSocket = null;
let selectedTraceId = null;
const traceCards = new Map();
const titles = {overview:"Overview",trace:"Live Trace",sessions:"Conversations / Sessions",routing:"Routing",models:"Models / STT",voice:"TTS / Voice",skills:"Skills",tools:"Tools",usage:"Usage",system:"System",logs:"Logs",security:"Security / Credential Status",passkeys:"Passkeys / Authentication",actions:"Actions / Broker",capabilities:"Capabilities",codex:"Codex Jobs"};

// Codes that mean the browser session itself is gone. Everything else -- an
// expired elevation above all -- leaves the session usable, and must not be
// reported as "your session is invalid", which was the cryptic dead end that
// made the Admin page look broken.
const SESSION_DEAD_CODES = ["invalid_session", "authentication_session_invalid",
                            "session_expired", "session_identity_denied"];

class ApiError extends Error {
  constructor(code, message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.code = code || "request_failed";
    this.status = status;
    this.payload = payload || {};
  }
  // A privileged action refused only because temporary elevation lapsed. The
  // fix is one passkey ceremony, not a reload.
  get requiresElevation() {
    return this.code === "elevation_required" || this.payload.reauthorize === "elevation";
  }
  get sessionDead() { return SESSION_DEAD_CODES.includes(this.code); }
}

let sessionDead = false;

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-Butters-CSRF", csrf);
  let response;
  try {
    response = await fetch(path, {...options, headers, credentials:"same-origin"});
  } catch (cause) {
    // A dropped connection or a restarting daemon is not an authorization
    // problem, and previously surfaced as a bare "Failed to fetch".
    throw new ApiError("network_unreachable",
      "Butters could not be reached. It may be restarting; retry in a moment.", 0);
  }
  const contentType = response.headers.get("content-type") || "";
  let value;
  try {
    value = contentType.includes("json") ? await response.json() : await response.blob();
  } catch (cause) {
    if (response.ok) throw new ApiError("malformed_response", "Butters returned an unreadable response.", response.status);
    value = {};
  }
  if (!response.ok) {
    const error = new ApiError(value.error, value.message || `Request failed: ${response.status}`,
                               response.status, value);
    if (error.sessionDead) announceSessionExpired();
    throw error;
  }
  return {value, response};
}

// -------------------------------------------------------- session recovery UI
//
// An expired browser session used to leave every control on the page in place,
// failing only once clicked. It is now announced once, at the top of the page,
// with the existing login path, and the page stops issuing further requests.
function announceSessionExpired() {
  if (sessionDead) return;
  sessionDead = true;
  const banner = ensureBanner();
  banner.className = "warning-card admin-banner";
  banner.replaceChildren();
  const text = document.createElement("span");
  text.textContent = "Your browser session expired. No action was retried. " +
    "Reload to sign in again; the Admin page will return to this state.";
  const reload = document.createElement("button");
  reload.className = "primary-button";
  reload.type = "button";
  reload.textContent = "Reload and sign in";
  reload.addEventListener("click", () => location.reload());
  banner.append(text, reload);
  banner.hidden = false;
  document.querySelector("#admin-status").textContent = "Session expired";
  // Stale controls that would only fail on click are disabled up front.
  document.querySelectorAll(".admin-main button").forEach(button => {
    if (button !== reload) button.disabled = true;
  });
}

function ensureBanner() {
  let banner = document.querySelector("#admin-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "admin-banner";
    banner.hidden = true;
    document.querySelector(".admin-main").prepend(banner);
  }
  return banner;
}

function showNotice(message) {
  const banner = ensureBanner();
  if (sessionDead) return;
  banner.className = "warning-card admin-banner";
  banner.replaceChildren(document.createTextNode(message));
  banner.hidden = false;
}

function clearNotice() {
  const banner = document.querySelector("#admin-banner");
  if (banner && !sessionDead) banner.hidden = true;
}

// Panel failures are reported without destroying the authorization pill, which
// previously became the error text and never recovered.
function reportPanelError(error) {
  if (error instanceof ApiError && error.sessionDead) return;
  showNotice(describeError(error));
}

function describeError(error) {
  if (error instanceof ApiError && error.requiresElevation) {
    return error.message + " Use Authenticate in Passkeys / Authentication, or click the action again.";
  }
  return (error && error.message) || "An unexpected error occurred.";
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
  clearNotice();
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
  } catch(error) {
    // A denied identity is a different condition from an expired session and
    // from a transport failure; only the first is a permanent "Denied".
    if (error instanceof ApiError && error.sessionDead) return;
    document.querySelector("#admin-status").textContent =
      error instanceof ApiError && error.status === 403 ? "Denied" : "Unavailable";
    reportPanelError(error);
  }
}

async function refresh(panel) {
  try {
    if(panel==="overview") cards(document.querySelector("#overview-grid"),(await api("/api/admin/overview")).value);
    if(panel==="trace") {await refreshTraces();connectTraceSocket();}
    if(panel==="sessions") rows(document.querySelector("#session-list"),(await api("/api/admin/sessions")).value.sessions,item=>[item.session_id,`${item.message_count} messages · idle ${item.idle_seconds}s · ${item.context_chars} chars`]);
    if(panel==="models") renderObject(document.querySelector("#model-status"),(await api("/api/admin/models")).value);
    if(panel==="voice") await refreshVoice();
    if(panel==="skills") await refreshSkills();
    if(panel==="tools") {await refreshDesktop();const data=(await api("/api/admin/tools")).value; rows(document.querySelector("#tool-list"),data.tools,item=>[item.name,`${item.action_class} · ${item.timeout_seconds}s · ${item.description}`]);}
    if(panel==="usage") renderObject(document.querySelector("#usage-view"),(await api("/api/admin/usage")).value);
    if(panel==="system") renderObject(document.querySelector("#system-view"),(await api("/api/admin/system")).value);
    if(panel==="logs") document.querySelector("#logs-view").textContent=pretty((await api("/api/admin/logs")).value);
    if(panel==="security") renderObject(document.querySelector("#security-view"),(await api("/api/admin/security")).value);
    if(panel==="passkeys") await refreshPasskeys();
    if(panel==="actions") renderObject(document.querySelector("#action-admin-view"),(await api("/api/admin/actions")).value);
    if(panel==="capabilities") renderCapabilities((await api("/api/capabilities")).value.capabilities);
    if(panel==="codex") await refreshJobs();
  } catch(error) { reportPanelError(error); }
}

function renderObject(container,value){rows(container,Object.entries(value),item=>[item[0].replaceAll("_"," "),typeof item[1]==="object"?pretty(item[1]):String(item[1])]);}

function decodeBase64url(value){const base64=value.replaceAll("-","+").replaceAll("_","/")+"=".repeat((4-value.length%4)%4);const binary=atob(base64);return Uint8Array.from(binary,character=>character.charCodeAt(0));}
function encodeBase64url(value){const bytes=new Uint8Array(value);let binary="";for(const byte of bytes)binary+=String.fromCharCode(byte);return btoa(binary).replaceAll("+","-").replaceAll("/","_").replaceAll("=","");}
function authOptions(value){const options={...value,challenge:decodeBase64url(value.challenge)};if(Array.isArray(value.allowCredentials))options.allowCredentials=value.allowCredentials.map(item=>({...item,id:decodeBase64url(item.id)}));return options;}
function registrationOptions(value){const options={...value,challenge:decodeBase64url(value.challenge),user:{...value.user,id:decodeBase64url(value.user.id)}};if(Array.isArray(value.excludeCredentials))options.excludeCredentials=value.excludeCredentials.map(item=>({...item,id:decodeBase64url(item.id)}));return options;}
function assertionJson(credential){return{id:credential.id,rawId:encodeBase64url(credential.rawId),type:credential.type,authenticatorAttachment:credential.authenticatorAttachment||null,clientExtensionResults:credential.getClientExtensionResults(),response:{authenticatorData:encodeBase64url(credential.response.authenticatorData),clientDataJSON:encodeBase64url(credential.response.clientDataJSON),signature:encodeBase64url(credential.response.signature),userHandle:credential.response.userHandle?encodeBase64url(credential.response.userHandle):null}};}
function registrationJson(credential){const transports=typeof credential.response.getTransports==="function"?credential.response.getTransports():[];return{id:credential.id,rawId:encodeBase64url(credential.rawId),type:credential.type,authenticatorAttachment:credential.authenticatorAttachment||null,clientExtensionResults:credential.getClientExtensionResults(),response:{attestationObject:encodeBase64url(credential.response.attestationObject),clientDataJSON:encodeBase64url(credential.response.clientDataJSON),transports}};}
async function authenticatePurpose(purpose="elevation",subject=null){if(!window.PublicKeyCredential||!navigator.credentials)throw new Error("Passkeys are unavailable in this browser");const body={purpose};if(subject)body.subject=subject;const begin=(await api("/api/auth/authenticate/options",{method:"POST",body:JSON.stringify(body)})).value;const credential=await navigator.credentials.get({publicKey:authOptions(begin.publicKey)});if(!credential)throw new Error("Authentication cancelled");return(await api("/api/auth/authenticate/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:assertionJson(credential)})})).value;}
function authStatusText(status){return status.elevated?`Elevated for ${status.remaining_seconds}s (server authoritative, non-sliding)`:`Locked · ${status.passkey_count} active passkey(s)`;}
async function refreshPasskeys(){const status=(await api("/api/auth/status")).value;document.querySelector("#auth-admin-status").textContent=authStatusText(status);const credentials=(await api("/api/auth/passkeys")).value.credentials;const container=document.querySelector("#passkey-list");container.replaceChildren();for(const credential of credentials){const row=document.createElement("article");row.className="data-row";const content=document.createElement("div");const title=document.createElement("h3");title.textContent=credential.label;const detail=document.createElement("p");detail.textContent=`Created ${new Date(credential.created_at*1000).toLocaleString()} · last used ${credential.last_used_at?new Date(credential.last_used_at*1000).toLocaleString():"never"}${credential.revoked?" · revoked":""}`;content.append(title,detail);row.append(content);if(!credential.revoked){const revoke=document.createElement("button");revoke.className="secondary-button";revoke.textContent="Revoke";revoke.addEventListener("click",()=>revokePasskey(credential.record_id));row.append(revoke);}container.append(row);}}
async function revokePasskey(recordId){if(!confirm("Revoke this passkey after a fresh assertion?"))return;try{const outcome=await authenticatePurpose("revoke_passkey",recordId);await api("/api/auth/passkeys/revoke",{method:"POST",body:JSON.stringify({record_id:recordId,fresh_grant:outcome.fresh_grant})});await refreshPasskeys();}catch(error){alert(error.message);}}
async function addPasskey(){try{const status=(await api("/api/auth/status")).value;const label=prompt("Passkey label","iPhone passkey");if(!label)return;let bootstrapToken=null;let freshGrant=null;if(status.passkey_count===0){bootstrapToken=prompt("Enter the short-lived token generated locally on the Pi");if(!bootstrapToken)return;}else{freshGrant=(await authenticatePurpose("register_passkey")).fresh_grant;}const begin=(await api("/api/auth/passkeys/register/options",{method:"POST",body:JSON.stringify({label,bootstrap_token:bootstrapToken,fresh_grant:freshGrant})})).value;const credential=await navigator.credentials.create({publicKey:registrationOptions(begin.publicKey)});if(!credential)throw new Error("Registration cancelled");await api("/api/auth/passkeys/register/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:registrationJson(credential)})});await refreshPasskeys();}catch(error){alert(error.message);}}
function renderCapabilities(values){rows(document.querySelector("#capability-list"),values,item=>[item.name,`${item.action_class} · auth ${item.authentication} · ${item.available?"available":item.unavailable_reason||"unavailable"}`]);}
document.querySelector("#admin-authenticate").addEventListener("click",async()=>{try{await authenticatePurpose();await refreshPasskeys();}catch(error){alert(error.message);}});
document.querySelector("#admin-lock").addEventListener("click",async()=>{try{await api("/api/auth/lock",{method:"POST"});await refreshPasskeys();}catch(error){alert(error.message);}});
document.querySelector("#add-passkey").addEventListener("click",addPasskey);

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

async function refreshVoice(){const data=(await api("/api/admin/voice/presets")).value;rows(document.querySelector("#voice-presets"),data.presets,item=>[item.name,`${item.provider} · ${item.model} · ${item.voice} · ${item.speed}x${item.is_default?" · default":""}`]);}
document.querySelector("#voice-provider").addEventListener("change",event=>{const cloud=event.target.value==="openai";document.querySelector("#voice-model").value=cloud?"gpt-4o-mini-tts":"local-piper";document.querySelector("#voice-name").value=cloud?"cedar":"kathleen";});
document.querySelector("#voice-form").addEventListener("submit",async event=>{event.preventDefault();try{const body=voiceBody();body.phrase=document.querySelector("#voice-phrase").value;const {value}=await api("/api/admin/voice/preview",{method:"POST",body:JSON.stringify(body)});const audio=document.querySelector("#voice-audio");audio.src=URL.createObjectURL(value);await audio.play();}catch(error){alert(error.message);}});
document.querySelector("#save-preset").addEventListener("click",async()=>{const name=prompt("Preset name","Butters default");if(!name)return;const body={...voiceBody(),name,make_default:true};try{await api("/api/admin/voice/presets",{method:"POST",body:JSON.stringify(body)});await refreshVoice();}catch(error){alert(error.message);}});
function voiceBody(){return{name:"preview",provider:document.querySelector("#voice-provider").value,model:document.querySelector("#voice-model").value,voice:document.querySelector("#voice-name").value,speed:Number(document.querySelector("#voice-speed").value),instructions:document.querySelector("#voice-instructions").value};}

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

// ============================================================= Tools panel ===
//
// Every subsection below refreshes and fails independently. The previous
// version wrapped host status, agent status, applications and VMs in one
// try/catch whose only output was #desktop-status, so a single rejected
// desktop.app.list request -- which is what an expired elevation or a backend
// that had never registered the action produced -- simultaneously removed the
// application controls and replaced the host status with an authorization
// error. One transient failure made the whole panel look broken.

let desktopProjects = [];
let desktopBusy = false;
let desktopAgent = null;
let elevated = false;
let desktopRegistered = {};
let desktopReachable = null;
let desktopHostname = "";
// The registry the agent last reported. Keeping it means a registered
// application stays represented, and explains itself, while the desktop is
// off -- instead of the controls appearing to materialise out of nowhere the
// moment the agent connects.
let knownApps = [];

// The four control states. Anything not `available` stays visible with a
// reason rather than vanishing or silently doing nothing. The exception is
// `requires_authorization`, which stays clickable, because clicking it is how
// the user re-elevates.
const CONTROL_LABELS = {
  available: "",
  unavailable: "Temporarily unavailable",
  not_configured: "Not configured",
  requires_authorization: "Authorization required",
};

function applyControlState(button, state, reason) {
  const enabled = state === "available" || state === "requires_authorization";
  button.disabled = desktopBusy || sessionDead || !enabled;
  button.dataset.controlState = state;
  button.title = reason || CONTROL_LABELS[state] || "";
  button.setAttribute("aria-disabled", String(button.disabled));
  // The state also gets a visible word, not only a hover title and a colour:
  // a phone has no hover, and a colour alone is not a message.
  if (CONTROL_LABELS[state]) button.dataset.controlLabel = CONTROL_LABELS[state];
  else delete button.dataset.controlLabel;
}

// A control that needs elevation is offered, not hidden. Hiding it was how the
// Tools page came to look as though the desktop controls did not exist.
function privilegedState(availableReason) {
  return elevated ? ["available", availableReason]
                  : ["requires_authorization",
                     "Renewed passkey authorization is required; clicking will request it."];
}

async function refreshAuthState() {
  try {
    const status = (await api("/api/auth/status")).value;
    elevated = status.elevated === true;
    return status;
  } catch (error) {
    // Not knowing the elevation state must not hide controls; assume it is
    // needed and let the action itself request it.
    elevated = false;
    throw error;
  }
}

// ---- interaction feedback --------------------------------------------------
//
// The complaint this answers was "it feels like I'm pressing an image": the
// press produced nothing until the network came back. Pressed and focus states
// are CSS; the loading state is here, because it has to name what is running
// and it has to be undone on every exit path, including a thrown error.

let activeControl = null;
let outcomeTimer = null;

function markBusy(button, label) {
  activeControl = button || null;
  if (!button) return;
  if (button.dataset.idleLabel === undefined) button.dataset.idleLabel = button.textContent;
  if (label) button.textContent = label;
  button.dataset.busy = "true";
  button.setAttribute("aria-busy", "true");
}

function clearBusy() {
  const button = activeControl;
  activeControl = null;
  if (!button) return;
  if (button.dataset.idleLabel !== undefined) {
    button.textContent = button.dataset.idleLabel;
    delete button.dataset.idleLabel;
  }
  delete button.dataset.busy;
  button.removeAttribute("aria-busy");
}

// A brief, non-blocking confirmation on the control that was pressed. The
// durable record of what happened is the result summary, so losing this flash
// -- on a control that a refresh re-rendered -- costs no information.
function flashOutcome(button, ok) {
  if (!button || !button.isConnected) return;
  clearTimeout(outcomeTimer);
  button.dataset.outcome = ok ? "success" : "failure";
  outcomeTimer = setTimeout(() => { delete button.dataset.outcome; }, 2400);
}

// ---- stage lists -----------------------------------------------------------
//
// Wake and shutdown both report a sequence of stages Butters can actually
// observe. Reached, awaited and unobserved stages differ by glyph as well as
// by colour, and each carries a screen-reader word, so none of the three
// depends on seeing a hue.
const STAGE_MARKS = {done: "✓", active: "•", pending: "○"};
const STAGE_WORDS = {done: "observed", active: "waiting", pending: "not observed"};

function renderStageList(target, stages, reached, note, active) {
  target.replaceChildren();
  for (const [label] of stages) {
    const tone = reached.includes(label) ? "done" : label === active ? "active" : "pending";
    const item = document.createElement("li");
    item.dataset.stage = tone;
    const mark = document.createElement("span");
    mark.className = "stage-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = STAGE_MARKS[tone];
    const text = document.createElement("span");
    text.textContent = label;
    const spoken = document.createElement("span");
    spoken.className = "visually-hidden";
    spoken.textContent = ` (${STAGE_WORDS[tone]})`;
    item.append(mark, text, spoken);
    target.append(item);
  }
  if (note) {
    const item = document.createElement("li");
    item.className = "stage-note";
    item.textContent = note;
    target.append(item);
  }
}

function stageProgress(stages, state) {
  const reached = stages.filter(([, test]) => test(state)).map(([label]) => label);
  const next = stages.map(([label]) => label).find(label => !reached.includes(label));
  return [reached, next];
}

// ---- result presentation ---------------------------------------------------
//
// The panel used to lead with a pretty-printed job object and leave the reader
// to work out that `visible_window: true, session_id: 1` meant the window was
// up. It now leads with a sentence and keeps the object, unabridged, under
// Technical details -- nothing diagnostic is discarded.

const APP_TITLES = {git_bash: "Git Bash", parsec: "Parsec", vs_code: "VS Code",
                    vscode: "VS Code", explorer: "File Explorer"};

function appTitle(name) {
  if (APP_TITLES[name]) return APP_TITLES[name];
  return String(name || "application").replace(/[_-]+/g, " ")
    .replace(/\b\w/g, character => character.toUpperCase());
}

// Unwrap only the layers the action path genuinely produces: a coordinator job
// wraps per-step skill results, and a structured skill result wraps its data.
function actionPayload(value) {
  if (!value || typeof value !== "object") return {};
  const steps = value.result && value.result.steps;
  if (Array.isArray(steps) && steps.length) {
    return actionPayload(steps[steps.length - 1].result);
  }
  if (value.kind && value.data && typeof value.data === "object") return value.data;
  return value;
}

function summarizeAction(action, parameters, result) {
  // A job that did not reach `completed` is reported as exactly that, with the
  // backend's own failure reason rather than a guess.
  if (result && typeof result === "object" && result.state
      && ["failed", "cancelled", "expired"].includes(result.state)) {
    return {ok: false, text: result.failure_reason || result.failure_code
      || `The ${action} job ended ${result.state}.`};
  }
  const data = actionPayload(result);
  if (action === "desktop.app.launch") {
    const title = appTitle(parameters.app);
    if (data.success === false) {
      return {ok: false, text: `${title} did not launch: ` +
        `${data.error || data.reason || "the Desktop Agent rejected the launch"}.`};
    }
    const session = data.session_id === undefined || data.session_id === null
      ? "" : ` in Windows session ${data.session_id}`;
    if (data.state === "already_running") {
      return {ok: true, text: `${title} already running${session}.`};
    }
    return {ok: true, text: `${title} launched${session}.`};
  }
  if (action === "wake_desktop") {
    return data.accepted === false
      ? {ok: false, text: "The broker did not accept the wake request."}
      : {ok: true, text: "Desktop wake request succeeded."};
  }
  if (action === "shutdown_desktop") {
    return data.accepted === false
      ? {ok: false, text: "The broker did not accept the shutdown request."}
      : {ok: true, text: "Desktop shutdown request accepted by the desktop."};
  }
  if (action === "desktop.ssh_test") {
    return data.success
      ? {ok: true, text: "SSH reached the desktop and its sentinel matched."}
      : {ok: false, text: `SSH did not complete: ${data.stderr || "no sentinel response"}.`};
  }
  if (action === "desktop.streaming.prepare") {
    return data.success === false
      ? {ok: false, text: `Streaming preparation stopped: ${data.error || "see details"}.`}
      : {ok: true, text: "Desktop prepared for streaming."};
  }
  if (action && action.startsWith("desktop.") && data.exit_code !== undefined) {
    const seconds = data.duration_seconds === undefined ? "" : ` in ${data.duration_seconds}s`;
    return data.success
      ? {ok: true, text: `${action} completed${seconds} (exit code 0).`}
      : {ok: false, text: `${action} failed${seconds} with exit code ${data.exit_code}.`};
  }
  if (data.success === false) {
    return {ok: false, text: `${action} did not succeed: ${data.error || "see details"}.`};
  }
  return {ok: true, text: `${action} completed.`};
}

const RESULT_MARKS = {success: "✓", failure: "✕", pending: "…"};

function showResult(summary, raw) {
  const line = document.querySelector("#desktop-result-summary");
  const tone = summary.ok === null || summary.ok === undefined
    ? "pending" : summary.ok ? "success" : "failure";
  line.dataset.outcome = tone;
  line.replaceChildren();
  const mark = document.createElement("span");
  mark.className = "result-mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = RESULT_MARKS[tone];
  const text = document.createElement("span");
  text.textContent = summary.text;
  line.append(mark, text);
  if (raw !== undefined) {
    document.querySelector("#desktop-result").textContent = pretty(raw);
  }
}

// While a job is queued or running, report the coordinator's own state, stage
// and progress rather than an invented timer.
function describeJob(job) {
  const percent = typeof job.progress === "number"
    ? ` · ${Math.round(job.progress * 100)}%` : "";
  return `Job ${job.state}${job.stage ? ` · ${job.stage}` : ""}${percent}…`;
}

function desktopButtons() {
  const project = desktopProjects.find(item => item.name === document.querySelector("#desktop-project").value);
  document.querySelectorAll("[data-compute]").forEach(button => {
    const build = button.dataset.compute !== "desktop.test";
    const test = button.dataset.compute !== "desktop.compile";
    if (!project) {
      applyControlState(button, "not_configured", "Register a verified project in desktop-compute.toml first.");
    } else if ((build && !project.build_available) || (test && !project.test_available)) {
      applyControlState(button, "not_configured", `Project ${project.name} declares no matching command.`);
    } else {
      applyControlState(button, ...privilegedState(`Run on ${project.name}`));
    }
  });
  applyControlState(document.querySelector("#desktop-refresh"), "available", "Re-read desktop state");
  applyControlState(document.querySelector("#desktop-ssh-test"), "available", "Read-only SSH reachability check");
  applyControlState(document.querySelector("#desktop-wake"), ...wakeControlState());
  applyControlState(document.querySelector("#desktop-shutdown"), ...shutdownControlState());
}

// Wake is offered when the desktop is unreachable, and stays visible and
// explained when it is not needed or not configured, so the control is
// discoverable rather than appearing only in the state where it is useful.
function wakeControlState() {
  const registered = desktopRegistered.wake_desktop;
  if (!registered) {
    return ["not_configured", "Wake is not registered on this Butters build."];
  }
  if (!registered.available || !registered.enabled) {
    return ["not_configured", registered.unavailable_reason
      || "Wake is disabled, or the action broker is unprovisioned."];
  }
  if (desktopReachable === true) {
    // Not an error, and not hidden: sending another packet would simply be
    // redundant. The reason says so.
    return ["unavailable", "The desktop is already reachable; no wake is needed."];
  }
  return privilegedState(desktopReachable === false
    ? "Send the configured Wake-on-LAN packet to the desktop."
    : "Reachability is unknown; sending wake is safe and idempotent.");
}

// Shutdown is the same shape of control, one risk tier up. It is never
// reported as available-without-authorization, because the registered skill is
// FRESH-authenticated: every run freezes a plan, is confirmed against that
// plan, and is then satisfied with a passkey assertion bound to its digest.
function shutdownControlState() {
  const registered = desktopRegistered.shutdown_desktop;
  if (!registered) {
    return ["not_configured", "Shutdown is not registered on this Butters build."];
  }
  if (!registered.available || !registered.enabled) {
    return ["not_configured", registered.unavailable_reason
      || "Shutdown is disabled, or the action broker is unprovisioned."];
  }
  if (desktopReachable === false) {
    return ["unavailable", "The desktop is already offline; there is nothing to shut down."];
  }
  return ["available",
    "Freeze a shutdown plan. You confirm the plan, then authorize it with a passkey."];
}

// The stages Butters can actually observe, in the order they become true.
// Rendered from real status, never from an assumption about what wake did.
const WAKE_STAGES = [
  ["wake requested", state => state.requested],
  ["magic packet sent", state => state.sent],
  ["waiting for desktop", state => state.waiting],
  ["network reachable", state => state.network],
  ["SSH available", state => state.ssh],
  ["agent connected", state => state.agent],
];

function renderWakeProgress(state) {
  const target = document.querySelector("#desktop-wake-progress");
  const reached = WAKE_STAGES.filter(([, test]) => test(state)).map(([label]) => label);
  const [, next] = stageProgress(WAKE_STAGES, state);
  renderStageList(target, WAKE_STAGES, reached, state.note, state.waiting ? next : null);
}

async function wakeDesktop() {
  if (desktopBusy || sessionDead) return;   // repeated clicks are dropped
  if (desktopReachable === true) {
    renderWakeProgress({network: true, ssh: null,
                        note: "The desktop was already reachable; no packet was sent."});
    showResult({ok: true, text: "Desktop already reachable. No wake packet was sent."});
    return;
  }
  renderWakeProgress({requested: true, note: "requesting authorization if required"});
  // The same registered action, coordinator and audit path a spoken request
  // takes. runDesktop handles elevation, the pending-action ceremony, and job
  // polling, and restores button state on failure.
  if (!await runDesktop("wake_desktop")) {
    renderWakeProgress({requested: true,
      note: "wake was not performed; no packet was sent"});
    return;
  }
  await observeWakeProgress();
}

// After the packet is sent, report only what the existing status surface
// observes. Butters never claims a stage it has not seen.
async function observeWakeProgress() {
  const deadline = Date.now() + 120000;
  let state = {requested: true, sent: true, waiting: true};
  renderWakeProgress(state);
  while (Date.now() < deadline && !sessionDead) {
    let status;
    try {
      status = (await api("/api/desktop/status")).value;
    } catch (error) {
      renderWakeProgress({...state, note: `status unavailable: ${describeError(error)}`});
      return;
    }
    const axes = status.axes || {};
    state = {
      requested: true,
      sent: true,
      waiting: !status.online,
      network: status.online === true,
      ssh: status.ssh_reachable === true,
      agent: status.agent_connected === true,
    };
    desktopReachable = typeof status.online === "boolean" ? status.online : null;
    if (state.agent) {
      renderWakeProgress({...state, waiting: false, note: "desktop and agent are ready"});
      showResult({ok: true, text: "Desktop awake. SSH is up and the Desktop Agent is connected."});
      return;
    }
    if (state.ssh) {
      renderWakeProgress({...state, waiting: false,
        note: `SSH is up; Windows session ${axes.session || "UNKNOWN"}, agent ${axes.agent || "OFFLINE"}`});
      showResult({ok: true, text: "Desktop awake and answering SSH. The Desktop Agent has not connected yet."});
      return;
    }
    renderWakeProgress(state);
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
  // The observation window closing is not a wake failure. The broker accepted
  // the request; the desktop simply has not been seen yet.
  renderWakeProgress({...state,
    note: "Wake sent. Desktop is still starting or has not yet become reachable."});
  showResult({ok: true,
    text: "Wake sent. Desktop is still starting or has not yet become reachable."});
}

// ---- shutdown --------------------------------------------------------------
//
// Shutdown reuses the registered `shutdown_desktop` skill, which already
// reaches the root broker's one fixed desktop-control operation. There is no
// second implementation and no command, host, address or parameter the browser
// can supply: the backend rejects parameters for a registered action and takes
// the machine from configuration. Because the skill is FRESH-authenticated the
// backend always returns a frozen plan first, which is what the confirmation
// step below confirms.
const SHUTDOWN_STAGES = [
  ["shutdown requested", state => state.requested],
  ["confirmed and authorized", state => state.authorized],
  ["waiting for desktop to go offline", state => state.waiting],
  ["desktop offline", state => state.offline],
];

function renderShutdownProgress(state) {
  const target = document.querySelector("#desktop-shutdown-progress");
  const [reached, next] = stageProgress(SHUTDOWN_STAGES, state);
  renderStageList(target, SHUTDOWN_STAGES, reached, state.note, state.waiting ? next : null);
}

// The confirmation is a step in the existing ceremony, not a browser-only
// gate: the plan being confirmed is the one the coordinator already froze, and
// declining releases it through the existing cancel endpoint.
function confirmShutdown(plan) {
  const panel = document.querySelector("#desktop-shutdown-confirm");
  panel.replaceChildren();
  const question = document.createElement("p");
  question.className = "confirm-question";
  question.textContent = `Shut down ${desktopHostname || "the desktop"}? ` +
    "This ends every interactive session, including Parsec, and any running build.";
  const detail = document.createElement("p");
  detail.className = "control-reason";
  const steps = (plan.steps || []).map(step => step.skill).join(", ");
  detail.textContent = `Frozen plan: ${steps || plan.summary || "shutdown_desktop"}` +
    ` · ${plan.state === "pending_confirmation" ? "awaiting your confirmation" : plan.state}` +
    " · confirming requests a fresh passkey assertion bound to this plan.";
  const row = document.createElement("div");
  row.className = "button-row";
  const go = document.createElement("button");
  go.className = "danger-button";
  go.type = "button";
  go.textContent = "Confirm shutdown";
  const stop = document.createElement("button");
  stop.className = "secondary-button";
  stop.type = "button";
  stop.textContent = "Cancel";
  row.append(go, stop);
  panel.append(question, detail, row);
  panel.hidden = false;
  go.focus();
  return new Promise(resolve => {
    const settle = value => {
      panel.hidden = true;
      panel.replaceChildren();
      document.querySelector("#desktop-shutdown").focus();
      resolve(value);
    };
    go.addEventListener("click", () => settle(true));
    stop.addEventListener("click", () => settle(false));
    // Escape declines, which is what a keyboard user expects of a confirmation.
    panel.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.stopPropagation(); settle(false); }
    });
  });
}

async function cancelPendingAction(planId) {
  try {
    await api(`/api/actions/pending/${encodeURIComponent(planId)}/cancel`, {method: "POST"});
  } catch (error) {
    // The frozen plan expires by itself, so a failed cancel is reportable but
    // never leaves a runnable plan behind.
    reportPanelError(error);
  }
}

async function shutdownDesktop() {
  if (desktopBusy || sessionDead) return;   // repeated clicks are dropped
  if (desktopReachable === false) {
    renderShutdownProgress({offline: true,
      note: "Desktop already offline; no shutdown was requested."});
    showResult({ok: true, text: "Desktop already offline. Nothing was requested."});
    return;
  }
  renderShutdownProgress({requested: true, note: "freezing a plan for confirmation"});
  const button = document.querySelector("#desktop-shutdown");
  const ran = await runDesktop("shutdown_desktop", {}, {
    origin: button,
    busyLabel: "Shutting down…",
    pendingLabel: "Freezing a shutdown plan for confirmation…",
    confirm: confirmShutdown,
  });
  if (!ran) {
    renderShutdownProgress({requested: true,
      note: "shutdown was not performed; the desktop is untouched"});
    return;
  }
  await observeShutdownProgress();
}

// Shutdown is reported from observation, not from the broker's acceptance. The
// desktop is called offline only once it stops answering ping and SSH.
async function observeShutdownProgress() {
  const deadline = Date.now() + 120000;
  let state = {requested: true, authorized: true, waiting: true};
  renderShutdownProgress(state);
  while (Date.now() < deadline && !sessionDead) {
    let status;
    try {
      status = (await api("/api/desktop/status")).value;
    } catch (error) {
      renderShutdownProgress({...state, note: `status unavailable: ${describeError(error)}`});
      return;
    }
    desktopReachable = typeof status.online === "boolean" ? status.online : null;
    if (status.online === false) {
      renderShutdownProgress({requested: true, authorized: true, waiting: false, offline: true,
        note: "the desktop stopped answering ping and SSH"});
      showResult({ok: true, text: "Desktop offline. The shutdown completed."});
      await refreshDesktop();
      return;
    }
    renderShutdownProgress(state);
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
  // Accurate rather than optimistic: the request was accepted, and the machine
  // is still answering, which is what Windows looks like while it closes
  // applications. Claiming success here would be a claim Butters cannot make.
  renderShutdownProgress({...state,
    note: "Shutdown accepted. The desktop is still reachable and has not gone offline yet."});
  showResult({ok: null,
    text: "Shutdown accepted. The desktop is still reachable and has not confirmed it went offline."});
}

// ---- host status -----------------------------------------------------------
//
// The backend already reports discrete power/network/os/session/agent axes.
// Rendering them separately, rather than collapsing to one word, is the point:
// "powered off" and "reachable but no interactive session" are different
// operational situations with different next steps. They are now laid out as a
// grid of labelled chips so the five can be read at a glance, which is a
// presentation change only -- no axis was merged away.
const AXIS_LABELS = {power:"Power", network:"Network", os:"SSH / OS", session:"Windows session", agent:"Desktop Agent"};
const AXIS_TONE = {
  RESPONDING: "good", REACHABLE: "good", AUTHENTICATED: "good", CONNECTED: "good",
  ACTIVE: "good", UNLOCKED: "good",
  SSH_RESPONDING: "partial", LOCKED: "partial", AWAITING_HEARTBEAT: "partial",
  UNREACHABLE: "bad", OFFLINE: "bad", NONE: "bad", DISCONNECTED: "bad",
};

function axisTone(value) {
  return AXIS_TONE[String(value).toUpperCase()] || "unknown";
}

async function refreshDesktopStatus() {
  const status = document.querySelector("#desktop-status");
  const headline = document.querySelector("#desktop-summary");
  try {
    const result = (await api("/api/desktop/status")).value;
    // online is true, false, or absent when the probe could not decide.
    desktopReachable = typeof result.online === "boolean" ? result.online : null;
    desktopHostname = result.hostname || "desktop";
    status.replaceChildren();
    const axes = result.axes || {};
    for (const [key, label] of Object.entries(AXIS_LABELS)) {
      if (!(key in axes)) continue;
      const item = document.createElement("div");
      item.className = "axis";
      item.dataset.tone = axisTone(axes[key]);
      const name = document.createElement("span");
      name.className = "axis-label";
      name.textContent = label;
      const value = document.createElement("span");
      value.className = "axis-value";
      value.textContent = axes[key];
      item.append(name, value);
      status.append(item);
    }
    if (!status.childElementCount) {
      status.textContent = "This backend reported no per-axis desktop state.";
    }
    const note = result.status_note || result.stderr;
    headline.replaceChildren();
    const host = document.createElement("strong");
    host.textContent = desktopHostname;
    headline.append(host);
    const summary = document.createElement("span");
    summary.textContent = ` · ${desktopReachable === true ? "reachable"
      : desktopReachable === false ? "not reachable" : "reachability unknown"}` +
      (note ? ` — ${note}` : "");
    headline.append(summary);
  } catch (error) {
    status.textContent = `Desktop host status unavailable: ${describeError(error)}`;
    headline.textContent = "Desktop state could not be read.";
    throw error;
  }
}

async function refreshDesktopCatalog() {
  const catalog = (await api("/api/desktop/catalog")).value;
  desktopProjects = catalog.projects || [];
  desktopRegistered = Object.fromEntries(
    (catalog.registered_actions || []).map(item => [item.action, item]));
  const select = document.querySelector("#desktop-project");
  const previous = select.value;
  select.replaceChildren(...(desktopProjects.length
    ? desktopProjects.map(item => new Option(item.name, item.name))
    : [new Option("No registered projects", "")]));
  if (desktopProjects.some(item => item.name === previous)) select.value = previous;
  select.disabled = !desktopProjects.length;
  if (catalog.configuration_error) {
    document.querySelector("#desktop-compute-note").textContent = catalog.configuration_error;
  }
  return catalog;
}

// Sections are refreshed concurrently and settled independently, so one
// rejection cannot prevent the others from rendering.
async function refreshDesktop() {
  if (sessionDead) return;
  let catalog = null;
  const outcomes = await Promise.allSettled([
    refreshAuthState(),
    refreshDesktopCatalog().then(value => { catalog = value; }),
    refreshDesktopStatus(),
  ]);
  // The agent subsection needs the catalog, so it runs once that has settled.
  // A missing desktop_agent key means the backend predates the agent work --
  // report that plainly instead of throwing a TypeError into the console.
  await refreshDesktopAgent(catalog ? catalog.desktop_agent : null);
  desktopButtons();
  const failure = outcomes.find(item => item.status === "rejected");
  if (failure) reportPanelError(failure.reason); else clearNotice();
}

// ---- Desktop Agent, applications and VMs -----------------------------------
const AGENT_STATE_TEXT = {
  not_configured: "No Desktop Agent is configured on this Butters host.",
  disconnected: "Not connected. The desktop is powered off, asleep, or the agent is not running.",
  awaiting_heartbeat: "Connected, waiting for the first authenticated heartbeat.",
  heartbeat_stale: "Heartbeat is stale; the agent is treated as gone.",
  heartbeat_aging: "Heartbeat is aging; GUI launch is withheld until it recovers.",
  connected: "Connected.",
};

function describeAgent(agent) {
  if (!agent) return "Desktop Agent state is unavailable from this backend.";
  const base = AGENT_STATE_TEXT[agent.state] || agent.reason || "State unknown.";
  const parts = [base];
  if (agent.version) parts.push(`v${agent.version}`);
  if (agent.session && agent.session.state) parts.push(`session ${agent.session.state}`);
  if (agent.last_heartbeat_age_seconds !== null && agent.last_heartbeat_age_seconds !== undefined) {
    parts.push(`heartbeat ${agent.last_heartbeat_age_seconds}s ago`);
  } else {
    parts.push("no heartbeat observed");
  }
  return parts.join(" · ");
}

function appControlState(app, agent) {
  if (!agent || !agent.agent_connected) {
    return ["unavailable", describeAgent(agent)];
  }
  if (app.installed === false) {
    return ["not_configured", app.reason || "Not installed at its registered path on the desktop."];
  }
  if (!(agent.capabilities && agent.capabilities.gui_launch)) {
    return ["unavailable", "An active, unlocked interactive Windows session is required."];
  }
  return privilegedState(app.running
    ? "Already running; launching again is idempotent."
    : "Launch this registered application.");
}

async function refreshDesktopAgent(agent) {
  desktopAgent = agent;
  const area = document.querySelector("#desktop-apps");
  const vms = document.querySelector("#desktop-vms");
  area.replaceChildren();
  document.querySelector("#desktop-agent-status").textContent = describeAgent(agent);

  const streaming = document.querySelector("#desktop-streaming");
  if (!agent || !agent.agent_connected) {
    applyControlState(streaming, "unavailable", describeAgent(agent));
  } else {
    applyControlState(streaming, ...privilegedState("Wake, ensure Parsec, and launch the streaming desktop."));
  }

  if (!agent || !agent.agent_connected) {
    // The registry the agent last reported keeps the capability represented
    // and explained, instead of the controls seeming to appear at random.
    renderAppCards(area, knownApps, agent, true);
    vms.textContent = agent && agent.state === "not_configured"
      ? "No VM backend is configured."
      : "VM status is unavailable while the agent is disconnected.";
    return;
  }

  await renderApplications(area, agent);
  await renderVms(vms);
}

async function renderApplications(area, agent) {
  let data;
  try {
    const response = (await api("/api/desktop/actions",
      {method:"POST", body:JSON.stringify({action:"desktop.app.list", parameters:{}})})).value;
    data = response.data || response;
  } catch (error) {
    // Listing applications is read-only. If it fails, say so here and leave
    // the rest of the panel -- host status, SSH test, compute -- working.
    area.textContent = `Application list unavailable: ${describeError(error)}`;
    reportPanelError(error);
    return;
  }
  if (data.success === false) {
    area.textContent = `Application list unavailable: ${data.error || "the agent rejected the request"}`;
    return;
  }
  const apps = data.apps || [];
  if (apps.length) knownApps = apps;
  renderAppCards(area, apps, agent, false);
}

function renderAppCards(area, apps, agent, remembered) {
  area.replaceChildren();
  if (!apps.length) {
    const note = document.createElement("p");
    note.className = "tool-note";
    note.textContent = remembered
      ? "Registered GUI controls are listed once the agent reconnects. " +
        "SSH compute and wake remain available and independent."
      : "The agent reports no registered applications. Add entries to its apps.toml.";
    area.append(note);
    return;
  }
  for (const app of apps) {
    const [state, reason] = appControlState(app, agent);
    const card = document.createElement("div");
    card.className = "app-card";
    card.dataset.controlState = state;

    const head = document.createElement("div");
    head.className = "app-head";
    const name = document.createElement("strong");
    name.textContent = appTitle(app.app);
    const chip = document.createElement("span");
    chip.className = "app-chip";
    // While the agent is gone, the honest condition is that the control is
    // waiting for it -- not a stale "running" from the last time it answered.
    const condition = remembered ? "waiting for Desktop Agent"
      : app.installed === false ? "not installed"
      : app.running ? "running" : "stopped";
    chip.dataset.condition = remembered ? "waiting"
      : app.installed === false ? "missing"
      : app.running ? "running" : "stopped";
    chip.textContent = condition +
      (!remembered && app.instances ? ` (${app.instances})` : "");
    head.append(name, chip);

    const button = document.createElement("button");
    button.className = "secondary-button app-launch";
    button.type = "button";
    button.textContent = `Launch ${appTitle(app.app)}`;
    applyControlState(button, state, reason);
    button.addEventListener("click", () => runDesktop("desktop.app.launch", {app: app.app},
      {origin: button, busyLabel: "Launching…",
       pendingLabel: `Launching ${appTitle(app.app)}…`}));

    card.append(head, button);
    if (state !== "available") {
      const why = document.createElement("small");
      why.className = "control-reason";
      why.textContent = `${CONTROL_LABELS[state]}: ${reason}`;
      card.append(why);
    }
    area.append(card);
  }
  if (remembered) {
    const note = document.createElement("p");
    note.className = "tool-note";
    note.textContent = "Shown from the registry the Desktop Agent last reported. " +
      "Launching resumes when it reconnects; wake and SSH compute are independent.";
    area.append(note);
  }
}

async function renderVms(target) {
  try {
    const response = (await api("/api/desktop/actions",
      {method:"POST", body:JSON.stringify({action:"desktop.vm.list", parameters:{}})})).value;
    const vms = response.data || response;
    const list = vms.vms || [];
    target.textContent = list.length
      ? list.map(item => `${item.vm}: ${item.state || "unknown"}`).join(" · ")
      : (vms.reason || vms.error || "No VM backend is configured on the desktop.");
  } catch (error) {
    target.textContent = `VM status unavailable: ${describeError(error)}`;
  }
}

// ---- running an action -----------------------------------------------------
async function runDesktop(action, parameters = {}, options = {}) {
  if (desktopBusy || sessionDead) return;   // no duplicate submissions
  desktopBusy = true;
  // The pressed control names what it is doing before the first byte moves.
  markBusy(options.origin, options.busyLabel);
  desktopButtons();
  document.querySelectorAll("#desktop-apps button, #desktop-streaming").forEach(b => { b.disabled = true; });
  const output = document.querySelector("#desktop-result");
  showResult({ok: null, text: options.pendingLabel || `Running ${action}…`});
  let succeeded = false;
  try {
    const result = await executeDesktop(action, parameters, output, options);
    const summary = summarizeAction(action, parameters, result);
    showResult(summary, result);
    flashOutcome(options.origin, summary.ok !== false);
    // A completed job that reports a failed operation is not a success, and
    // callers that observe afterwards must not treat it as one.
    succeeded = summary.ok !== false;
  } catch (error) {
    // Say what actually happened. "Git Bash launch requires renewed
    // authorization" is actionable; "session invalid or expired" was not, and
    // was not even true.
    const text = error instanceof ApiError && error.requiresElevation
      ? `${action} requires renewed passkey authorization. Nothing was run. ` +
        `Click the action again, or use Authenticate in Passkeys / Authentication.`
      : describeError(error);
    showResult({ok: false, text},
               error instanceof ApiError ? error.payload : {error: String(error && error.message)});
    flashOutcome(options.origin, false);
    reportPanelError(error);
  } finally {
    // Button state is always restored, including after a failure.
    desktopBusy = false;
    clearBusy();
    await refreshDesktop();
  }
  // Callers that follow an action with observation need to know whether it ran,
  // so a cancelled ceremony is never reported as progress.
  return succeeded;
}

async function executeDesktop(action, parameters, output, options = {}) {
  let result;
  try {
    result = (await api("/api/desktop/actions",
      {method:"POST", body:JSON.stringify({action, parameters})})).value;
  } catch (error) {
    // Elevation expired between rendering and clicking: run the existing
    // ceremony and retry exactly once, rather than dead-ending.
    if (!(error instanceof ApiError) || !error.requiresElevation) throw error;
    showResult({ok: null, text: "Renewed authorization required. Confirm with your passkey…"});
    await authenticatePurpose("elevation");
    elevated = true;
    result = (await api("/api/desktop/actions",
      {method:"POST", body:JSON.stringify({action, parameters})})).value;
  }
  // A privileged action returns a frozen plan instead of running. Satisfying
  // it with a passkey assertion is the designed elevation path.
  if (result.pending_action) {
    // A higher-risk action is confirmed against the frozen plan before any
    // ceremony starts. Declining releases the plan through the existing
    // endpoint instead of leaving it to expire unexplained.
    if (options.confirm && !await options.confirm(result.pending_action)) {
      await cancelPendingAction(result.pending_action.pending_action_id);
      throw new Error("Cancelled at the confirmation step. Nothing was run.");
    }
    showResult({ok: null, text: "Confirm this action with your passkey…"});
    const begin = (await api("/api/auth/authenticate/options", {method:"POST",
      body:JSON.stringify({purpose:"pending_action",
                           pending_action_id:result.pending_action.pending_action_id})})).value;
    const credential = await navigator.credentials.get({publicKey:authOptions(begin.publicKey)});
    if (!credential) throw new Error("Authentication was cancelled. Nothing was run.");
    result = (await api("/api/auth/authenticate/verify", {method:"POST",
      body:JSON.stringify({ceremony_id:begin.ceremony_id,
                           credential:assertionJson(credential)})})).value;
    elevated = true;
  }
  for (const initial of result.jobs || result.action_jobs || []) {
    let job = initial;
    for (let poll = 0; poll < 310 && ["queued", "running", "waiting"].includes(job.state); poll++) {
      // The summary reports the coordinator's own state and stage; the raw job
      // stays live under Technical details.
      showResult({ok: null, text: describeJob(job)});
      output.textContent = pretty(job);
      await new Promise(resolve => setTimeout(resolve, 1000));
      // One request per poll: the previous form issued a second identical
      // request whenever the payload had no `job` key.
      const payload = (await api("/api/actions/jobs/" + encodeURIComponent(job.job_id))).value;
      job = payload.job || payload;
    }
    result = job;
  }
  return result;
}

document.querySelector("#desktop-refresh").addEventListener("click", refreshDesktop);
document.querySelector("#desktop-ssh-test").addEventListener("click",
  () => runDesktop("desktop.ssh_test", {},
    {origin: document.querySelector("#desktop-ssh-test"), busyLabel: "Testing…",
     pendingLabel: "Running a read-only SSH reachability check…"}));
document.querySelector("#desktop-wake").addEventListener("click", wakeDesktop);
document.querySelector("#desktop-shutdown").addEventListener("click", shutdownDesktop);
document.querySelector("#desktop-project").addEventListener("change", desktopButtons);
document.querySelectorAll("[data-compute]").forEach(button => button.addEventListener("click",
  () => runDesktop(button.dataset.compute, {project:document.querySelector("#desktop-project").value},
    {origin: button, busyLabel: "Running…"})));
document.querySelector("#desktop-streaming").addEventListener("click",
  () => runDesktop("desktop.streaming.prepare", {},
    {origin: document.querySelector("#desktop-streaming"), busyLabel: "Preparing…",
     pendingLabel: "Preparing the desktop for streaming…"}));

initialize();
