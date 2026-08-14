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
// This is a backstop against a lost HTTP response, so it must never fire on a
// request the service will still answer. cloud.max_wall_seconds (90s) only
// gates entry to a tool round, so the last round starts under that budget and
// then runs cloud.timeout_seconds (45s) once per attempt, retries included:
// 90 + 45 * (max_retries + 1) = 180s worst case under the shipped config.
// Aborting earlier would report a timeout for a turn the server still stores.
const CHAT_LIMIT_MS = 240000;
// A media element that never reports an outcome must not hold a turn open.
const PLAYBACK_LIMIT_MS = 120000;
let csrf = "";
let socket = null;
let mediaStream = null;
let audioContext = null;
let processor = null;
let sourceNode = null;
let currentPlayback = null;
let holding = false;
let voiceTurn = 0;
let voiceReadyResolve = null;
let pending = false;
let currentTurn = 0;

function setState(label, key = "idle") {
  stateLabel.textContent = label;
  stateLabel.dataset.state = key;
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

async function playResponse(turn, traceId) {
  if (!isCurrentTurn(turn)) return;
  if (traceId) {
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
    const response = await fetch("/api/speech", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Butters-CSRF": csrf},
      body: JSON.stringify({trace_id: traceId}),
    });
    if (!response.ok) return;
    const blob = await response.blob();
    if (!blob.size || playback.stopped || currentPlayback !== playback) return;
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

function setMicActive(active) {
  micButton.classList.toggle("active", active);
  micButton.setAttribute("aria-pressed", active ? "true" : "false");
}

async function beginVoice(event) {
  event.preventDefault();
  if (holding || !csrf) return;
  cleanupVoice();
  holding = true;
  const turn = beginTurn();
  voiceTurn = turn;
  stopPlayback();
  setMicActive(true);
  setState("Requesting microphone", "listening");
  try {
    const requestedStream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}, video: false});
    if (!holding || !isCurrentTurn(turn) || voiceTurn !== turn) {
      requestedStream.getTracks().forEach(track => track.stop());
      return;
    }
    mediaStream = requestedStream;
    const requestedContext = new (window.AudioContext || window.webkitAudioContext)();
    audioContext = requestedContext;
    await requestedContext.resume();
    if (!holding || !isCurrentTurn(turn) || voiceTurn !== turn) {
      requestedStream.getTracks().forEach(track => track.stop());
      requestedContext.close().catch(() => {});
      return;
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws/voice`);
    socket.binaryType = "arraybuffer";
    socket.onmessage = message => handleVoiceEvent(message, turn);
    socket.onerror = () => {
      if (isCurrentTurn(turn) && voiceTurn === turn) {
        setState("Voice connection error", "error");
      }
    };
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = reject;
    });
    const ready = new Promise((resolve, reject) => {
      voiceReadyResolve = resolve;
      window.setTimeout(() => reject(new Error("STT startup timed out")), 15000);
    });
    socket.send(JSON.stringify({type: "start", csrf_token: csrf, sample_rate: audioContext.sampleRate, channels: 1, encoding: "pcm_s16le"}));
    await ready;
    voiceReadyResolve = null;
    if (!holding || !isCurrentTurn(turn) || voiceTurn !== turn) {
      cleanupVoice(turn);
      return;
    }
    sourceNode = audioContext.createMediaStreamSource(mediaStream);
    processor = audioContext.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = audioEvent => {
      if (!socket || socket.readyState !== WebSocket.OPEN || !holding) return;
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
    setState("Listening", "listening");
  } catch (_) {
    if (isCurrentTurn(turn) && voiceTurn === turn) {
      setState("Microphone unavailable", "error");
    }
    cleanupVoice(turn);
  }
}

async function endVoice(event) {
  // The holding check comes first because this also runs for every pointer
  // release on the page: cancelling the default action of an unrelated tap
  // suppresses the activation and focus behaviour of ordinary controls.
  if (!holding) return;
  if (event) event.preventDefault();
  holding = false;
  setMicActive(false);
  if (processor) processor.onaudioprocess = null;
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({type: "stop"}));
    setState("Transcribing", "processing");
  }
  if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
  if (sourceNode) sourceNode.disconnect();
  if (processor) processor.disconnect();
}

function handleVoiceEvent(event, turn) {
  if (!isCurrentTurn(turn) || voiceTurn !== turn) return;
  let data;
  try {
    data = JSON.parse(event.data);
  } catch (_) {
    setState("Voice error", "error");
    cleanupVoice(turn);
    return;
  }
  if (data.type === "listening" && voiceReadyResolve) voiceReadyResolve();
  if (data.type === "speech_start") setState("Listening", "listening");
  if (data.type === "partial") {
    partial.hidden = false;
    partial.textContent = data.text;
  }
  if (data.type === "final") {
    partial.hidden = true;
    if (data.raw_text) addMessage("user", data.raw_text);
    setState("Processing", "processing");
  }
  if (data.type === "assistant") {
    addMessage("assistant", data.response_text || "");
    playResponse(turn, typeof data.trace_id === "string" ? data.trace_id : null);
  }
  if (data.type === "error") {
    partial.hidden = true;
    addMessage("assistant", data.message || "Voice request failed safely.");
    setState("Voice error", "error");
  }
  if (["assistant", "error", "cancelled"].includes(data.type)) cleanupVoice(turn);
}

function cleanupVoice(expectedTurn = null) {
  if (expectedTurn !== null && voiceTurn !== expectedTurn) return;
  if (processor) {
    processor.onaudioprocess = null;
    try {
      processor.disconnect();
    } catch (_) {
      // Repeated cleanup is expected when a new turn supersedes voice input.
    }
  }
  if (sourceNode) {
    try {
      sourceNode.disconnect();
    } catch (_) {
      // The source may already have been disconnected by pointer release.
    }
  }
  if (mediaStream) {
    mediaStream.getTracks().forEach(track => {
      try {
        track.stop();
      } catch (_) {
        // A stopped track is already in the desired terminal state.
      }
    });
  }
  if (audioContext) {
    try {
      const closing = audioContext.close();
      if (closing && typeof closing.catch === "function") closing.catch(() => {});
    } catch (_) {
      // Cleanup must not make a subsequent text submission fail.
    }
  }
  if (socket) {
    socket.onmessage = null;
    socket.onopen = null;
    socket.onerror = null;
    if ([WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) {
      try {
        socket.close();
      } catch (_) {
        // Detaching the handlers still prevents a stale callback from winning.
      }
    }
  }
  socket = mediaStream = audioContext = processor = sourceNode = null;
  voiceReadyResolve = null;
  voiceTurn = 0;
  holding = false;
  setMicActive(false);
}

micButton.addEventListener("pointerdown", beginVoice);
window.addEventListener("pointerup", endVoice);
// A cancelled pointer never produces pointerup, so without this the hold would
// stay armed after a scroll or a system gesture interrupts the press.
window.addEventListener("pointercancel", endVoice);
micButton.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) beginVoice(event); });
micButton.addEventListener("keyup", event => { if (["Enter", " "].includes(event.key)) endVoice(event); });
window.addEventListener("beforeunload", () => cleanupVoice());
setPending(false);
setMicActive(false);
initialize();
