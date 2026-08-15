"use strict";

let csrf = "";
let models = [];
let selectedSkill = null;
let selectedJob = null;
let traceSocket = null;
let selectedTraceId = null;
const traceCards = new Map();
const titles = {overview:"Overview",trace:"Live Trace",sessions:"Conversations / Sessions",routing:"Routing",models:"Models / STT",voice:"TTS / Voice",skills:"Skills",tools:"Tools",usage:"Usage",system:"System",logs:"Logs",security:"Security / Credential Status",codex:"Codex Jobs"};

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
    if(panel==="voice") await refreshVoice();
    if(panel==="skills") await refreshSkills();
    if(panel==="tools") {const data=(await api("/api/admin/tools")).value; rows(document.querySelector("#tool-list"),data.tools,item=>[item.name,`${item.action_class} · ${item.timeout_seconds}s · ${item.description}`]);}
    if(panel==="usage") renderObject(document.querySelector("#usage-view"),(await api("/api/admin/usage")).value);
    if(panel==="system") renderObject(document.querySelector("#system-view"),(await api("/api/admin/system")).value);
    if(panel==="logs") document.querySelector("#logs-view").textContent=pretty((await api("/api/admin/logs")).value);
    if(panel==="security") renderObject(document.querySelector("#security-view"),(await api("/api/admin/security")).value);
    if(panel==="codex") await refreshJobs();
  } catch(error) { document.querySelector("#admin-status").textContent=error.message; }
}

function renderObject(container,value){rows(container,Object.entries(value),item=>[item[0].replaceAll("_"," "),typeof item[1]==="object"?pretty(item[1]):String(item[1])]);}

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

initialize();
