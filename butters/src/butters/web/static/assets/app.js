"use strict";

const stateLabel = document.querySelector("#assistant-state");
const conversation = document.querySelector("#conversation");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const micButton = document.querySelector("#mic-button");
const sendButton = document.querySelector("#send-button");
const partial = document.querySelector("#partial");
const clearButton = document.querySelector("#clear-button");
const stopAudio = document.querySelector("#stop-audio");
const voiceOutputToggle = document.querySelector("#voice-output-toggle");
const authButton = document.querySelector("#auth-button");
const lockButton = document.querySelector("#lock-button");
const actionCard = document.querySelector("#action-card");
const actionTitle = document.querySelector("#action-title");
const actionSummary = document.querySelector("#action-summary");
const actionProgress = document.querySelector("#action-progress");
const actionAuthenticate = document.querySelector("#action-authenticate");
const actionCancel = document.querySelector("#action-cancel");
// This is a backstop against a lost HTTP response, so it must never fire on a
// request the service will still answer. cloud.max_wall_seconds (90s) only
// gates entry to a tool round, so the last round starts under that budget and
// then runs cloud.timeout_seconds (45s) once per attempt, retries included:
// 90 + 45 * (max_retries + 1) = 180s worst case under the shipped config.
// Aborting earlier would report a timeout for a turn the server still stores.
const CHAT_LIMIT_MS = 240000;
// A media element that never reports an outcome must not hold a turn open.
const PLAYBACK_LIMIT_MS = 120000;
const VOICE_OUTPUT_KEY = "butters.voice-output-enabled";
const VOICE_STATE = Object.freeze({
  IDLE: "idle",
  REQUESTING_PERMISSION: "requesting_permission",
  CONNECTING: "connecting",
  LISTENING: "listening",
  STOPPING: "stopping",
  TRANSCRIBING: "transcribing",
  ROUTING: "routing",
  ERROR: "error",
});
const VOICE_TIMEOUT_MS = Object.freeze({
  requesting_permission: 60000,
  connecting: 30000,
  listening: 30000,
  stopping: 5000,
  transcribing: 30000,
  routing: CHAT_LIMIT_MS,
});
let csrf = "";
let socket = null;
let mediaStream = null;
let audioContext = null;
let processor = null;
let sourceNode = null;
let currentPlayback = null;
let voiceTurn = 0;
let voiceReadyResolve = null;
let voiceReadyReject = null;
let voiceState = VOICE_STATE.IDLE;
let voiceStateTimer = 0;
let voiceSession = null;
let voiceOutputEnabled = true;
let pending = false;
let currentTurn = 0;
let pendingAction = null;
let activeJob = null;
let authExpiry = 0;

function setState(label, key = "idle") {
  stateLabel.textContent = label;
  stateLabel.dataset.state = key;
}

function loadVoiceOutputPreference() {
  try {
    const stored = window.localStorage.getItem(VOICE_OUTPUT_KEY);
    voiceOutputEnabled = stored === null ? true : stored === "true";
  } catch (_) {
    voiceOutputEnabled = true;
  }
  renderVoiceOutputPreference();
}

function renderVoiceOutputPreference() {
  const label = voiceOutputEnabled ? "Voice: On" : "Voice: Off";
  voiceOutputToggle.textContent = label;
  voiceOutputToggle.setAttribute("aria-checked", voiceOutputEnabled ? "true" : "false");
  voiceOutputToggle.setAttribute("aria-label", `Voice output ${voiceOutputEnabled ? "on" : "off"}`);
}

function setVoiceOutputPreference(enabled) {
  voiceOutputEnabled = Boolean(enabled);
  try {
    window.localStorage.setItem(VOICE_OUTPUT_KEY, String(voiceOutputEnabled));
  } catch (_) {
    // The visible in-memory preference remains authoritative for this page.
  }
  renderVoiceOutputPreference();
  if (!voiceOutputEnabled) stopPlayback();
}

// Only the newest exchange may write the header, so a reply that finishes
// speaking after the next question was asked cannot report "Ready" over it.
function beginTurn() {
  currentTurn += 1;
  return currentTurn;
}

function isCurrentTurn(turn) {
  return turn === currentTurn;
}

// The text field is never disabled. A pending request only blocks Send, so no
// request, playback, or rendering failure can leave the composer unusable.
function setPending(value) {
  pending = value;
  sendButton.disabled = value;
  form.dataset.pending = value ? "true" : "false";
}

function addMessage(role, text) {
  const item = document.createElement("article");
  item.className = `message ${role === "user" ? "user-message" : "assistant-message"}`;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  item.append(paragraph);
  conversation.append(item);
  conversation.scrollTop = conversation.scrollHeight;
}

async function readJson(response) {
  try {
    const data = await response.json();
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error();
    return data;
  } catch (_) {
    // A proxy error page or truncated body must fail closed, including on 2xx.
    throw new Error("Invalid server response");
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-Butters-CSRF", csrf);
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  const data = await readJson(response);
  if (!response.ok) throw new Error(data.message || "Request failed");
  return data;
}

async function initialize() {
  try {
    const response = await fetch("/api/session", {credentials: "same-origin"});
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "Session failed");
    if (typeof data.csrf_token !== "string" || !data.csrf_token) {
      throw new Error("Invalid server response");
    }
    csrf = data.csrf_token;
    if (Array.isArray(data.messages) && data.messages.length) {
      conversation.replaceChildren();
      for (const message of data.messages) addMessage(message.role, message.text);
    }
    setState("Ready", "idle");
    await refreshAuthStatus();
  } catch (error) {
    setState("Connection error", "error");
  }
}

async function sendText(text) {
  cleanupVoice();
  const turn = beginTurn();
  stopPlayback();
  addMessage("user", text);
  setState("Processing", "processing");
  setPending(true);
  let traceId = null;
  let requestTimer = 0;
  try {
    const controller = new AbortController();
    requestTimer = window.setTimeout(() => controller.abort(), CHAT_LIMIT_MS);
    const response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Butters-CSRF": csrf},
      body: JSON.stringify({text}),
      signal: controller.signal,
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error(data.message || "Request failed");
    if (typeof data.response_text !== "string" || !data.response_text.trim()) {
      throw new Error("Invalid server response");
    }
    addMessage("assistant", data.response_text);
    traceId = typeof data.trace_id === "string" ? data.trace_id : null;
    if (data.pending_action && typeof data.pending_action === "object") {
      showPendingAction(data.pending_action);
    } else if (Array.isArray(data.jobs) && data.jobs.length) {
      showJob(data.jobs[0]);
    }
  } catch (error) {
    const message = error && error.name === "AbortError"
      ? "The request timed out. Please try again."
      : (error && error.message) || "The request failed safely.";
    addMessage("assistant", message);
    if (isCurrentTurn(turn)) setState("Error", "error");
    return;
  } finally {
    // The composer recovers with the answer, never with the audio that may
    // follow it: spoken playback is a separate, non-blocking phase.
    if (requestTimer) window.clearTimeout(requestTimer);
    setPending(false);
  }
  await playResponse(turn, traceId);
}

function decodeBase64url(value) {
  const base64 = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(base64);
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

function encodeBase64url(value) {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function authenticationOptions(value) {
  const options = {...value, challenge: decodeBase64url(value.challenge)};
  if (Array.isArray(value.allowCredentials)) {
    options.allowCredentials = value.allowCredentials.map(item => ({...item, id: decodeBase64url(item.id)}));
  }
  return options;
}

function credentialJson(credential) {
  return {
    id: credential.id,
    rawId: encodeBase64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || null,
    clientExtensionResults: credential.getClientExtensionResults(),
    response: {
      authenticatorData: encodeBase64url(credential.response.authenticatorData),
      clientDataJSON: encodeBase64url(credential.response.clientDataJSON),
      signature: encodeBase64url(credential.response.signature),
      userHandle: credential.response.userHandle ? encodeBase64url(credential.response.userHandle) : null,
    },
  };
}

async function usePasskey(purpose = "elevation", pendingActionId = null, subject = null) {
  if (!window.PublicKeyCredential || !navigator.credentials) throw new Error("Passkeys are unavailable in this browser");
  const body = {purpose};
  if (pendingActionId) body.pending_action_id = pendingActionId;
  if (subject) body.subject = subject;
  const begin = await api("/api/auth/authenticate/options", {method: "POST", body: JSON.stringify(body)});
  const assertion = await navigator.credentials.get({publicKey: authenticationOptions(begin.publicKey)});
  if (!assertion) throw new Error("Passkey authentication was cancelled");
  return api("/api/auth/authenticate/verify", {
    method: "POST",
    body: JSON.stringify({ceremony_id: begin.ceremony_id, credential: credentialJson(assertion)}),
  });
}

function renderAuthStatus(status) {
  const elevated = Boolean(status && status.elevated && Number(status.remaining_seconds) > 0);
  authExpiry = elevated ? Date.now() + Number(status.remaining_seconds) * 1000 : 0;
  authButton.hidden = elevated;
  lockButton.hidden = !elevated;
  authButton.textContent = "Admin: Locked · Authenticate";
  if (elevated) lockButton.textContent = `Admin: Elevated · ${formatRemaining(status.remaining_seconds)} · Lock Now`;
}

function formatRemaining(seconds) {
  const remaining = Math.max(0, Math.ceil(Number(seconds) || 0));
  return `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
}

async function refreshAuthStatus() {
  try {
    renderAuthStatus(await api("/api/auth/status"));
  } catch (_) {
    renderAuthStatus(null);
  }
}

function showPendingAction(plan) {
  pendingAction = plan;
  activeJob = null;
  actionCard.hidden = false;
  actionTitle.textContent = "Authentication required";
  actionSummary.textContent = typeof plan.summary === "string" ? plan.summary : "Sensitive action";
  actionProgress.textContent = `Requires ${String(plan.authentication || "passkey").toUpperCase()} authentication.`;
  actionAuthenticate.hidden = false;
  actionCancel.hidden = false;
}

function showJob(job) {
  pendingAction = null;
  activeJob = job;
  actionCard.hidden = false;
  actionAuthenticate.hidden = true;
  actionCancel.hidden = !["queued", "running", "waiting"].includes(job.state);
  actionTitle.textContent = "Action in progress";
  actionSummary.textContent = job.summary || job.skill || "Sensitive action";
  actionProgress.textContent = `${job.state} · ${job.stage || "queued"}`;
  pollJob(job.job_id);
}

async function pollJob(jobId) {
  if (!activeJob || activeJob.job_id !== jobId) return;
  try {
    const job = await api(`/api/actions/jobs/${encodeURIComponent(jobId)}`);
    if (!activeJob || activeJob.job_id !== jobId) return;
    activeJob = job;
    actionProgress.textContent = `${job.state} · ${job.stage || ""}`;
    actionCancel.hidden = !["queued", "running", "waiting"].includes(job.state);
    if (["completed", "failed", "cancelled", "expired"].includes(job.state)) {
      actionTitle.textContent = job.state === "completed" ? "Action completed" : "Action did not complete";
      if (job.failure_reason) actionProgress.textContent += ` · ${job.failure_reason}`;
      return;
    }
    window.setTimeout(() => pollJob(jobId), 1000);
  } catch (error) {
    actionTitle.textContent = "Action status unavailable";
    actionProgress.textContent = error.message;
  }
}

authButton.addEventListener("click", async () => {
  authButton.disabled = true;
  try {
    const result = await usePasskey();
    renderAuthStatus(result.status);
  } catch (error) {
    addMessage("assistant", error.message || "Passkey authentication failed safely.");
  } finally {
    authButton.disabled = false;
  }
});

lockButton.addEventListener("click", async () => {
  try {
    renderAuthStatus(await api("/api/auth/lock", {method: "POST"}));
  } catch (error) {
    addMessage("assistant", error.message || "Could not lock the elevated session.");
  }
});

actionAuthenticate.addEventListener("click", async () => {
  if (!pendingAction) return;
  actionAuthenticate.disabled = true;
  try {
    const result = await usePasskey("pending_action", pendingAction.pending_action_id);
    renderAuthStatus(result.status);
    if (Array.isArray(result.jobs) && result.jobs.length) showJob(result.jobs[0]);
  } catch (error) {
    actionProgress.textContent = error.message || "Authentication failed safely.";
  } finally {
    actionAuthenticate.disabled = false;
  }
});

actionCancel.addEventListener("click", async () => {
  try {
    if (pendingAction) {
      await api(`/api/actions/pending/${encodeURIComponent(pendingAction.pending_action_id)}/cancel`, {method: "POST"});
    } else if (activeJob) {
      await api(`/api/actions/jobs/${encodeURIComponent(activeJob.job_id)}/cancel`, {method: "POST"});
    }
    pendingAction = activeJob = null;
    actionCard.hidden = true;
  } catch (error) {
    actionProgress.textContent = error.message || "Cancellation is unavailable.";
  }
});

async function playResponse(turn, traceId) {
  if (!isCurrentTurn(turn)) return;
  if (traceId && voiceOutputEnabled) {
    setState("Speaking", "speaking");
    await speak(traceId);
  }
  if (isCurrentTurn(turn)) setState("Ready", "idle");
}

function finishPlayback(playback) {
  if (!playback || playback.settled) return;
  playback.settled = true;
  if (playback.timer) {
    window.clearTimeout(playback.timer);
    playback.timer = 0;
  }
  const settle = playback.settle;
  playback.settle = null;
  if (settle) settle();
}

function stopPlayback() {
  const playback = currentPlayback;
  if (playback) {
    playback.stopped = true;
    if (playback.audio) {
      try {
        playback.audio.pause();
      } catch (_) {
        // Settlement below is authoritative even if a media implementation fails.
      }
    }
    finishPlayback(playback);
    if (currentPlayback === playback) currentPlayback = null;
  }
  stopAudio.hidden = true;
}

async function speak(traceId) {
  // A response may arrive while an earlier speech fetch is still in flight.
  // Give each playback its own settlement state so stale media cannot resolve,
  // pause, or clear the newer response.
  stopPlayback();
  const playback = {
    audio: null,
    settle: null,
    settled: false,
    stopped: false,
    timer: 0,
  };
  currentPlayback = playback;
  let url = "";
  try {
    if (!voiceOutputEnabled) return;
    const response = await fetch("/api/speech", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Butters-CSRF": csrf},
      body: JSON.stringify({trace_id: traceId}),
    });
    if (!response.ok) return;
    const blob = await response.blob();
    if (!blob.size || !voiceOutputEnabled || playback.stopped || currentPlayback !== playback) return;
    url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    playback.audio = audio;
    stopAudio.hidden = false;
    await new Promise(resolve => {
      playback.settle = resolve;
      const finish = () => finishPlayback(playback);
      // Every terminal outcome settles this wait exactly once: a natural end, a
      // decode error, an explicit stop, a rejected autoplay attempt, and an iOS
      // interruption that pauses playback without ever emitting "ended".
      audio.addEventListener("ended", finish, {once: true});
      audio.addEventListener("error", finish, {once: true});
      audio.addEventListener("pause", finish, {once: true});
      playback.timer = window.setTimeout(finish, PLAYBACK_LIMIT_MS);
      const started = audio.play();
      if (started && typeof started.catch === "function") started.catch(finish);
    });
  } catch (_) {
    // Text response remains usable when local audio is not configured.
  } finally {
    finishPlayback(playback);
    if (playback.audio) {
      try {
        playback.audio.pause();
      } catch (_) {
        // The local playback is already settled; cleanup must remain idempotent.
      }
    }
    if (url) URL.revokeObjectURL(url);
    if (currentPlayback === playback) {
      stopAudio.hidden = true;
      currentPlayback = null;
    }
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  if (pending) return;
  const text = input.value.trim();
  if (!text) return;
  // Refocusing inside the submit gesture keeps an open keyboard up. Focusing
  // later, outside any gesture, is what leaves an iOS field looking focused
  // while refusing to open the keyboard on the next tap.
  const refocus = document.activeElement === input;
  input.value = "";
  if (refocus) input.focus();
  sendText(text);
});

clearButton.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/session/conversation", {
      method: "DELETE",
      credentials: "same-origin",
      headers: {"X-Butters-CSRF": csrf},
    });
    const data = await readJson(response);
    if (!response.ok) throw new Error();
    if (typeof data.csrf_token !== "string" || !data.csrf_token) throw new Error();
    csrf = data.csrf_token;
    conversation.replaceChildren();
    addMessage("assistant", "Conversation cleared.");
  } catch (_) {
    setState("Could not clear", "error");
  }
});

stopAudio.addEventListener("click", stopPlayback);
voiceOutputToggle.addEventListener("click", () => {
  setVoiceOutputPreference(!voiceOutputEnabled);
});

function setMicActive(active) {
  micButton.classList.toggle("active", active);
  micButton.setAttribute("aria-pressed", active ? "true" : "false");
}

const VOICE_TRANSITIONS = Object.freeze({
  idle: ["requesting_permission"],
  requesting_permission: ["connecting", "error", "idle"],
  connecting: ["listening", "error", "idle"],
  listening: ["stopping", "error", "idle"],
  stopping: ["transcribing", "error", "idle"],
  transcribing: ["routing", "error", "idle"],
  routing: ["error", "idle"],
  error: ["idle"],
});

function voiceIsCurrent(turn) {
  return Boolean(voiceSession) && voiceTurn === turn && isCurrentTurn(turn);
}

function clearVoiceStateTimer() {
  if (voiceStateTimer) window.clearTimeout(voiceStateTimer);
  voiceStateTimer = 0;
}

function showVoiceProgress(text) {
  partial.hidden = false;
  partial.textContent = text;
}

function transitionVoice(next, turn, label = null) {
  if (next !== VOICE_STATE.IDLE && next !== VOICE_STATE.ERROR && !voiceIsCurrent(turn)) return false;
  const allowed = VOICE_TRANSITIONS[voiceState] || [];
  if (next !== voiceState && !allowed.includes(next)) return false;
  clearVoiceStateTimer();
  voiceState = next;
  const listening = next === VOICE_STATE.LISTENING;
  setMicActive(listening);
  micButton.setAttribute("aria-label", listening ? "Stop recording" : next === VOICE_STATE.ERROR ? "Try voice input again" : "Start recording");
  micButton.title = listening ? "Tap to stop" : "Tap to record";

  const views = {
    requesting_permission: ["Requesting microphone", "listening"],
    connecting: ["Starting voice", "processing"],
    listening: ["Listening", "listening"],
    stopping: ["Stopping recording", "processing"],
    transcribing: ["Transcribing", "processing"],
    routing: ["Processing response", "processing"],
    idle: ["Ready", "idle"],
    error: [label || "Voice error", "error"],
  };
  const view = views[next];
  if (view) setState(view[0], view[1]);
  if (next === VOICE_STATE.LISTENING) showVoiceProgress("Listening…");
  if (next === VOICE_STATE.TRANSCRIBING) showVoiceProgress("Transcribing…");

  const timeout = VOICE_TIMEOUT_MS[next];
  if (timeout) {
    voiceStateTimer = window.setTimeout(() => {
      if (!voiceIsCurrent(turn) || voiceState !== next) return;
      if (next === VOICE_STATE.LISTENING) {
        stopVoice(null, turn, "maximum_duration");
      } else {
        const messages = {
          requesting_permission: "Microphone permission timed out.",
          connecting: "Voice startup timed out.",
          stopping: "Recording could not stop cleanly.",
          transcribing: "Transcription timed out. Please try again.",
          routing: "The voice response timed out. It will still appear after reload if the server completes it.",
        };
        failVoice(turn, messages[next] || "Voice processing timed out.");
      }
    }, timeout);
  }
  return true;
}

async function beginVoice(event) {
  if (event) event.preventDefault();
  if (!csrf || voiceState !== VOICE_STATE.IDLE) return;
  const turn = beginTurn();
  voiceTurn = turn;
  voiceSession = {
    turn,
    requestedAt: performance.now(),
    permissionMs: 0,
    connectedAt: 0,
    captureStartedAt: 0,
    finalShown: false,
    terminal: false,
  };
  stopPlayback();
  transitionVoice(VOICE_STATE.REQUESTING_PERMISSION, turn);
  try {
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      throw new Error("microphone capture is unavailable");
    }
    // Construct and unlock Web Audio inside the original tap.  In particular,
    // do not wait until Safari's permission sheet has consumed that gesture.
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error("audio processing is unavailable");
    audioContext = new AudioContextClass();
    await audioContext.resume();
    if (!voiceIsCurrent(turn)) return;

    const permissionStarted = performance.now();
    const requestedStream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true},
      video: false,
    });
    if (!voiceIsCurrent(turn) || voiceState !== VOICE_STATE.REQUESTING_PERMISSION) {
      requestedStream.getTracks().forEach(track => track.stop());
      return;
    }
    voiceSession.permissionMs = performance.now() - permissionStarted;
    mediaStream = requestedStream;
    // The permission sheet may suspend the already-authorized context. Resume
    // it again before wiring capture; this does not require a second mic tap.
    await audioContext.resume();
    if (!voiceIsCurrent(turn)) return;

    transitionVoice(VOICE_STATE.CONNECTING, turn);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws/voice`);
    socket.binaryType = "arraybuffer";
    socket.onmessage = message => handleVoiceEvent(message, turn);
    socket.onclose = () => {
      if (voiceIsCurrent(turn) && !voiceSession.terminal) {
        failVoice(turn, "Voice connection closed before the response.");
      }
    };
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, {once: true});
      socket.addEventListener("error", () => reject(new Error("voice connection failed")), {once: true});
      socket.addEventListener("close", () => reject(new Error("voice connection closed")), {once: true});
    });
    if (!voiceIsCurrent(turn) || voiceState !== VOICE_STATE.CONNECTING) return;
    voiceSession.connectedAt = performance.now();
    const ready = new Promise((resolve, reject) => {
      voiceReadyResolve = resolve;
      voiceReadyReject = reject;
    });
    socket.send(JSON.stringify({
      type: "start",
      csrf_token: csrf,
      sample_rate: audioContext.sampleRate,
      channels: 1,
      encoding: "pcm_s16le",
      client_permission_ms: Math.round(voiceSession.permissionMs),
      client_setup_ms: Math.round(performance.now() - voiceSession.requestedAt),
    }));
    await ready;
    voiceReadyResolve = voiceReadyReject = null;
    if (!voiceIsCurrent(turn) || voiceState !== VOICE_STATE.CONNECTING) return;

    sourceNode = audioContext.createMediaStreamSource(mediaStream);
    processor = audioContext.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = audioEvent => {
      if (!voiceIsCurrent(turn) || voiceState !== VOICE_STATE.LISTENING || !socket || socket.readyState !== WebSocket.OPEN) return;
      const samples = audioEvent.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(samples.length);
      for (let index = 0; index < samples.length; index += 1) {
        const sample = Math.max(-1, Math.min(1, samples[index]));
        pcm[index] = sample < 0 ? sample * 32768 : sample * 32767;
      }
      socket.send(pcm.buffer);
    };
    sourceNode.connect(processor);
    processor.connect(audioContext.destination);
    voiceSession.captureStartedAt = performance.now();
    transitionVoice(VOICE_STATE.LISTENING, turn);
  } catch (error) {
    if (!voiceIsCurrent(turn)) return;
    const denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
    failVoice(
      turn,
      denied ? "Microphone permission was denied." : "Microphone or voice connection is unavailable.",
      denied ? "Microphone permission denied" : "Microphone unavailable",
    );
  }
}

function stopCaptureResources() {
  if (processor) processor.onaudioprocess = null;
  if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
  if (sourceNode) {
    try { sourceNode.disconnect(); } catch (_) { /* already disconnected */ }
  }
  if (processor) {
    try { processor.disconnect(); } catch (_) { /* already disconnected */ }
  }
  if (audioContext) {
    try {
      const closing = audioContext.close();
      if (closing && typeof closing.catch === "function") closing.catch(() => {});
    } catch (_) {
      // The input stream has already stopped; cleanup remains idempotent.
    }
  }
  mediaStream = audioContext = processor = sourceNode = null;
}

function stopVoice(event, expectedTurn = voiceTurn, reason = "tap") {
  if (event) event.preventDefault();
  if (!voiceIsCurrent(expectedTurn) || voiceState !== VOICE_STATE.LISTENING) return;
  transitionVoice(VOICE_STATE.STOPPING, expectedTurn);
  const captureMs = voiceSession.captureStartedAt
    ? performance.now() - voiceSession.captureStartedAt
    : 0;
  stopCaptureResources();
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    failVoice(expectedTurn, "Voice connection was lost before transcription.");
    return;
  }
  socket.send(JSON.stringify({
    type: "stop",
    endpoint_reason: reason,
    client_capture_ms: Math.round(captureMs),
  }));
  transitionVoice(VOICE_STATE.TRANSCRIBING, expectedTurn);
}

function cancelVoice(turn = voiceTurn, message = "Voice request cancelled.") {
  if (!voiceIsCurrent(turn)) return;
  if (socket && socket.readyState === WebSocket.OPEN) {
    try { socket.send(JSON.stringify({type: "cancel"})); } catch (_) { /* cleanup closes it */ }
  }
  if (message) addMessage("assistant", message);
  cleanupVoice(turn);
}

function failVoice(turn, message, label = "Voice error") {
  if (!voiceIsCurrent(turn)) return;
  if (message) addMessage("assistant", message);
  transitionVoice(VOICE_STATE.ERROR, turn, label);
  cleanupVoice(turn, true);
}

function handleVoiceEvent(event, turn) {
  if (!voiceIsCurrent(turn)) return;
  let data;
  try {
    data = JSON.parse(event.data);
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error();
  } catch (_) {
    failVoice(turn, "The voice service returned an invalid response.");
    return;
  }
  if (data.type === "listening" && voiceReadyResolve) {
    voiceReadyResolve();
    return;
  }
  if (data.type === "speech_start" && voiceState === VOICE_STATE.LISTENING) {
    setState("Listening", "listening");
  }
  if (data.type === "partial") {
    if (typeof data.text === "string" && data.text.trim()) {
      showVoiceProgress(`Heard so far: ${data.text}`);
    }
  }
  if (data.type === "final") {
    partial.hidden = true;
    const finalText = typeof data.raw_text === "string" && data.raw_text.trim()
      ? data.raw_text.trim()
      : typeof data.normalized_text === "string" ? data.normalized_text.trim() : "";
    if (finalText && !voiceSession.finalShown) {
      addMessage("user", finalText);
      voiceSession.finalShown = true;
    }
    transitionVoice(VOICE_STATE.ROUTING, turn);
  }
  if (data.type === "assistant") {
    if (typeof data.response_text !== "string" || !data.response_text.trim()) {
      failVoice(turn, "The voice service returned an invalid response.");
      return;
    }
    // Text is committed to the visible conversation before optional TTS.
    addMessage("assistant", data.response_text);
    voiceSession.terminal = true;
    const traceId = typeof data.trace_id === "string" ? data.trace_id : null;
    cleanupVoice(turn);
    playResponse(turn, traceId);
    return;
  }
  if (data.type === "error") {
    failVoice(
      turn,
      typeof data.message === "string" && data.message.trim()
        ? data.message
        : "Voice request failed safely.",
    );
    return;
  }
  if (data.type === "cancelled") cleanupVoice(turn);
}

function cleanupVoice(expectedTurn = null, preserveError = false) {
  if (expectedTurn !== null && voiceTurn !== expectedTurn) return;
  clearVoiceStateTimer();
  stopCaptureResources();
  const rejectReady = voiceReadyReject;
  voiceReadyResolve = voiceReadyReject = null;
  if (voiceSession) voiceSession.terminal = true;
  if (socket) {
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    if ([WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) {
      try {
        socket.close();
      } catch (_) {
        // Detaching the handlers still prevents a stale callback from winning.
      }
    }
  }
  socket = null;
  voiceTurn = 0;
  voiceSession = null;
  if (rejectReady) rejectReady(new Error("voice session ended"));
  setMicActive(false);
  partial.hidden = true;
  partial.textContent = "";
  if (!preserveError) {
    voiceState = VOICE_STATE.IDLE;
    micButton.setAttribute("aria-label", "Start recording");
    micButton.title = "Tap to record";
    setState("Ready", "idle");
  }
}

function toggleVoice(event) {
  if (voiceState === VOICE_STATE.ERROR) {
    voiceState = VOICE_STATE.IDLE;
    beginVoice(event);
  } else if (voiceState === VOICE_STATE.IDLE) {
    beginVoice(event);
  } else if (voiceState === VOICE_STATE.LISTENING) {
    stopVoice(event);
  } else {
    if (event) event.preventDefault();
    cancelVoice();
  }
}

function handleVoicePointerCancel(event) {
  if (voiceState === VOICE_STATE.LISTENING) {
    stopVoice(event, voiceTurn, "pointer_cancel");
  } else if (![VOICE_STATE.IDLE, VOICE_STATE.ERROR].includes(voiceState)) {
    cancelVoice(voiceTurn, "Voice start cancelled.");
  }
}

micButton.addEventListener("click", toggleVoice);
micButton.addEventListener("pointercancel", handleVoicePointerCancel);
window.addEventListener("beforeunload", () => cleanupVoice());
setPending(false);
setMicActive(false);
loadVoiceOutputPreference();
initialize();
window.setInterval(() => {
  if (!authExpiry || lockButton.hidden) return;
  const remaining = Math.max(0, Math.ceil((authExpiry - Date.now()) / 1000));
  lockButton.textContent = `Admin: Elevated · ${formatRemaining(remaining)} · Lock Now`;
  if (remaining === 0) renderAuthStatus(null);
}, 1000);
window.setInterval(refreshAuthStatus, 30000);
