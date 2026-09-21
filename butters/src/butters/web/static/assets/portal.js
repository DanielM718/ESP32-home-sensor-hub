"use strict";

/* Jellyfin access portal.
 *
 * This client can sign in, read status, POST one wake, follow the destination
 * the server chooses, and—only when independently authorized—complete the
 * fixed NAS shutdown ceremony. It never names a host, address, action, method,
 * mode, delay, or redirect target. The only URL it will ever navigate to is the
 * server-returned Jellyfin destination.
 */

let csrf = "";
let pollTimer = null;
let pollStartedAt = null;
let maxPollSeconds = 300;

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-Butters-CSRF", csrf);
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || `Request failed: ${response.status}`);
  return value;
}

function decodeBase64url(value){const padded=value.replace(/-/g,"+").replace(/_/g,"/");const raw=atob(padded+"=".repeat((4-padded.length%4)%4));return Uint8Array.from(raw,character=>character.charCodeAt(0));}
function encodeBase64url(buffer){return btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");}
function authOptions(value){const options={...value,challenge:decodeBase64url(value.challenge)};if(Array.isArray(value.allowCredentials))options.allowCredentials=value.allowCredentials.map(item=>({...item,id:decodeBase64url(item.id)}));return options;}
function registrationOptions(value){const options={...value,challenge:decodeBase64url(value.challenge),user:{...value.user,id:decodeBase64url(value.user.id)}};if(Array.isArray(value.excludeCredentials))options.excludeCredentials=value.excludeCredentials.map(item=>({...item,id:decodeBase64url(item.id)}));return options;}
function assertionJson(c){return{id:c.id,rawId:encodeBase64url(c.rawId),type:c.type,authenticatorAttachment:c.authenticatorAttachment||null,clientExtensionResults:c.getClientExtensionResults(),response:{authenticatorData:encodeBase64url(c.response.authenticatorData),clientDataJSON:encodeBase64url(c.response.clientDataJSON),signature:encodeBase64url(c.response.signature),userHandle:c.response.userHandle?encodeBase64url(c.response.userHandle):null}};}
function registrationJson(c){const transports=typeof c.response.getTransports==="function"?c.response.getTransports():[];return{id:c.id,rawId:encodeBase64url(c.rawId),type:c.type,authenticatorAttachment:c.authenticatorAttachment||null,clientExtensionResults:c.getClientExtensionResults(),response:{attestationObject:encodeBase64url(c.response.attestationObject),clientDataJSON:encodeBase64url(c.response.clientDataJSON),transports}};}

const TONE={reachable:"good",ready:"good",unreachable:"bad",unavailable:"bad",starting:"warn",not_observed:"muted",dry_run:"info",observe:"info",off:"muted",estimated:"muted",measured:"good"};
const TEXT={reachable:"Reachable",unreachable:"Unreachable",ready:"Ready",starting:"Starting",unavailable:"Unavailable",not_observed:"Not observed",dry_run:"Dry run · nothing is limited",observe:"Observing only",off:"Off"};

function axis(container, entries){
  container.replaceChildren();
  for(const [label,raw] of entries){
    const cell=document.createElement("article"); cell.className=`axis-cell axis-${TONE[raw]||"muted"}`;
    const name=document.createElement("small"); name.textContent=label;
    const value=document.createElement("strong"); value.textContent=TEXT[raw]||String(raw);
    cell.append(name,value); container.append(cell);
  }
}

function mbps(value){return value===null||value===undefined?"Unavailable":`${Number(value).toFixed(1)} Mbps`;}

function renderBandwidth(value){
  const section=document.querySelector("#portal-bandwidth");
  if(!value){section.hidden=true;return;}
  section.hidden=false;
  // What someone in this house actually wants to know, in three cells: how
  // much room is left, how much is in use, and how many people are watching
  // from outside. Everything else is one disclosure away.
  axis(document.querySelector("#portal-bandwidth-summary"),[
    ["Room left",mbps(value.available_headroom_mbps)],
    ["In use now",value.total_remote_observed_mbps===null?"unavailable":`${mbps(value.total_remote_observed_mbps)} / ${mbps(value.effective_capacity_mbps)}`],
    ["Watching from away",value.remote_jellyfin_stream_count===null?"unavailable":String(value.remote_jellyfin_stream_count)],
  ]);
  axis(document.querySelector("#portal-bandwidth-metrics"),[
    ["Remote bandwidth",value.total_remote_observed_mbps===null?"unavailable":`${mbps(value.total_remote_observed_mbps)} / ${mbps(value.effective_capacity_mbps)}`],
    ["Safe streaming pool",mbps(value.safe_streaming_budget_mbps)],
    ["Jellyfin reported rate",mbps(value.remote_jellyfin_observed_mbps)],
    ["Other remote traffic",mbps(value.other_remote_observed_mbps)],
    ["Available headroom",mbps(value.available_headroom_mbps)],
    ["Remote streams",value.remote_jellyfin_stream_count===null?"unavailable":String(value.remote_jellyfin_stream_count)],
    ["Unknown streams",value.unknown_stream_count===null?"unavailable":String(value.unknown_stream_count)],
    ["Dry-run target",value.calculated_per_stream_target_mbps===null?(value.measurement_quality==="unavailable"?"unavailable":value.reason==="no_active_remote_or_unknown_streams"?"No active streams":"Stabilizing"):`${mbps(value.calculated_per_stream_target_mbps)} / stream`],
    ["Measurement quality",String(value.measurement_quality||"unavailable")],
    ["Policy mode",String(value.policy_mode||"off")],
  ]);
  const list=document.querySelector("#portal-remote-streams");
  list.replaceChildren();
  for(const stream of value.sessions||[]){
    if(stream.classification!=="remote")continue;
    const card=document.createElement("div");card.className="portal-stream";
    for(const text of [stream.user||"Viewer",stream.item||"Active item",stream.paused?"Paused":"Playing",String(stream.play_method||"unknown").replaceAll("_"," "),mbps(stream.observed_mbps)]){
      const line=document.createElement("span");line.textContent=text;card.append(line);
    }
    list.append(card);
  }
}

/* Wake really is a packet leaving this host, so it says so. Shutdown is a
 * request to the NAS Agent over an authenticated session -- not Wake-on-LAN --
 * so calling it a packet would describe the wrong mechanism. */
const POWER_ACTION_LABELS={wake:"Wake packet sent",shutdown:"Shutdown requested"};

function ago(seconds){if(seconds===null||seconds===undefined)return "";const value=Math.round(seconds);if(value<60)return `${value} second${value===1?"":"s"} ago`;const minutes=Math.round(value/60);return `${minutes} minute${minutes===1?"":"s"} ago`;}

function show(section){
  document.querySelector("#portal-signin").hidden = section!=="signin";
  document.querySelector("#portal-main").hidden = section!=="main";
}

async function initialize(){
  try{
    const session=await fetch("/api/session",{credentials:"same-origin"});
    const data=await session.json();
    if(!session.ok)throw new Error(data.message);
    csrf=data.csrf_token;
    const status=await api("/api/portal/status");
    document.querySelector("#portal-identity-line").textContent=
      status.authenticated?`Signed in as ${status.identity}`:`You are ${status.identity}`;
    if(status.authenticated){show("main"); await refreshState();}
    else{
      show("signin");
      document.querySelector("#portal-signin-status").textContent=
        status.enrolled?"":"This identity has no access yet. Ask for an enrollment invitation.";
    }
  }catch(error){
    document.querySelector("#portal-identity-line").textContent=error.message||"Unavailable";
    show("signin");
  }
}

async function signIn(){
  const status=document.querySelector("#portal-signin-status");
  try{
    if(!window.PublicKeyCredential||!navigator.credentials)throw new Error("Passkeys are unavailable in this browser");
    status.textContent="Waiting for your passkey…";
    const begin=(await api("/api/portal/authenticate/options",{method:"POST",body:JSON.stringify({})})).value;
    const credential=await navigator.credentials.get({publicKey:authOptions(begin.publicKey)});
    if(!credential)throw new Error("Sign-in cancelled");
    await api("/api/portal/authenticate/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:assertionJson(credential)})});
    status.textContent="";
    show("main"); await refreshState();
  }catch(error){status.textContent=error.message||"Sign-in failed";}
}

async function register(){
  const status=document.querySelector("#portal-signin-status");
  const label=document.querySelector("#portal-enroll-label").value.trim();
  const token=document.querySelector("#portal-enroll-token").value.trim();
  if(!label||!token){status.textContent="A label and an invitation are both required.";return;}
  try{
    status.textContent="Registering this device…";
    const begin=(await api("/api/portal/register/options",{method:"POST",body:JSON.stringify({label,invite_token:token})})).value;
    const credential=await navigator.credentials.create({publicKey:registrationOptions(begin.publicKey)});
    if(!credential)throw new Error("Registration cancelled");
    await api("/api/portal/register/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:registrationJson(credential)})});
    document.querySelector("#portal-enroll-token").value="";
    status.textContent="Registered. Now sign in with your passkey.";
  }catch(error){status.textContent=error.message||"Registration failed";}
}

async function refreshState(){
  try{
    const state=await api("/api/portal/nas?refresh=1");
    maxPollSeconds=state.max_poll_seconds||maxPollSeconds;
    document.querySelector("#portal-headline").textContent=state.headline;
    document.querySelector("#portal-detail").textContent=state.detail;
    axis(document.querySelector("#portal-observations"),[
      ["NAS (LAN)",state.observations.lan],
      ["NAS OS / API",state.observations.nas_api],
      ["Tailscale",state.observations.tailscale],
      ["Jellyfin",state.observations.jellyfin],
    ]);
    renderBandwidth(state.bandwidth);
    // Last operation is rendered on its own line and never changes the lines
    // above. The label comes from the server's `power_action` and from nothing
    // else -- not from the observations, which describe now rather than what
    // was asked for. An action this client does not recognise shows no line,
    // which is why the lookup drives `hidden` as well as the text.
    const last=document.querySelector("#portal-last-operation");
    const action=state.last_operation&&POWER_ACTION_LABELS[state.last_operation.power_action];
    last.hidden=!action;
    last.textContent=action?`${action} ${ago(state.last_operation.age_seconds)}`:"";
    const wake=document.querySelector("#portal-wake");
    const open=document.querySelector("#portal-open");
    const shutdown=document.querySelector("#portal-shutdown");
    // Wake Again only when it is actually appropriate, and never automatically.
    wake.hidden=!state.can_wake;
    open.hidden=!state.jellyfin_ready;
    shutdown.hidden=!state.can_shutdown;
    if(state.poll_expired){
      stopPolling();
      document.querySelector("#portal-action-status").textContent=
        `Jellyfin did not become ready within ${Math.round(maxPollSeconds/60)} minutes. The observations above are current. You can retry the status check, or wake again.`;
      return;
    }
    if(pollStartedAt!==null){
      document.querySelector("#portal-action-status").textContent=
        `Waiting… ${ago((Date.now()-pollStartedAt)/1000)||"just now"}`.replace(" ago","");
    }
  }catch(error){
    document.querySelector("#portal-action-status").textContent=error.message||"Status unavailable";
  }
}

async function enterJellyfin(){
  stopPolling();
  const status=document.querySelector("#portal-action-status");
  try{
    const destination=await api("/api/portal/destination");
    if(!destination.ready||!destination.destination){
      status.textContent="Jellyfin is ready but no destination is configured.";
      return;
    }
    status.textContent="Opening Jellyfin…";
    window.location.assign(destination.destination);
  }catch(error){status.textContent=error.message||"Could not open Jellyfin";}
}

function startPolling(){
  stopPolling();
  pollStartedAt=Date.now();
  // Polls status only. It never re-sends a wake packet.
  pollTimer=window.setInterval(refreshState,4000);
}

function stopPolling(){
  if(pollTimer!==null){window.clearInterval(pollTimer);pollTimer=null;}
}

async function wake(){
  const button=document.querySelector("#portal-wake");
  const status=document.querySelector("#portal-action-status");
  button.disabled=true;
  try{
    const result=await api("/api/portal/wake",{method:"POST",body:JSON.stringify({})});
    // The server's wording, kept verbatim: a packet was sent, nothing more.
    status.textContent=`${result.message}. Waiting for NAS…`;
    startPolling();
    await refreshState();
  }catch(error){status.textContent=error.message||"Wake failed";}
  finally{button.disabled=false;}
}

async function shutdownNas(){
  const button=document.querySelector("#portal-shutdown");
  const status=document.querySelector("#portal-action-status");
  if(!window.confirm("Shut down the NAS? Jellyfin will become unavailable."))return;
  button.disabled=true;
  try{
    if(!window.PublicKeyCredential||!navigator.credentials)throw new Error("Passkeys are unavailable in this browser");
    const plan=await api("/api/portal/shutdown/plan",{method:"POST",body:JSON.stringify({confirm:true})});
    const pending=plan.pending_action;
    status.textContent="Confirm this exact shutdown with your passkey…";
    const begin=(await api("/api/portal/shutdown/authenticate/options",{method:"POST",body:JSON.stringify({pending_action_id:pending.pending_action_id})})).value;
    const credential=await navigator.credentials.get({publicKey:authOptions(begin.publicKey)});
    if(!credential)throw new Error("Shutdown confirmation cancelled");
    await api("/api/portal/shutdown/authenticate/verify",{method:"POST",body:JSON.stringify({ceremony_id:begin.ceremony_id,credential:assertionJson(credential)})});
    status.textContent="Shutdown request queued. Waiting for observed state…";
    startPolling();
    await refreshState();
  }catch(error){status.textContent=error.message||"Shutdown request failed";}
  finally{button.disabled=false;}
}

document.querySelector("#portal-authenticate").addEventListener("click",signIn);
document.querySelector("#portal-register").addEventListener("click",register);
document.querySelector("#portal-wake").addEventListener("click",wake);
document.querySelector("#portal-open").addEventListener("click",enterJellyfin);
document.querySelector("#portal-shutdown").addEventListener("click",shutdownNas);
document.querySelector("#portal-retry").addEventListener("click",refreshState);
document.querySelector("#portal-signout").addEventListener("click",async()=>{
  stopPolling();
  try{await api("/api/portal/sign-out",{method:"POST",body:JSON.stringify({})});}catch(error){void error;}
  show("signin");
});

initialize();
