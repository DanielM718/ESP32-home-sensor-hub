"use strict";

const stateLabel = document.querySelector("#assistant-state");
const conversation = document.querySelector("#conversation");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const micButton = document.querySelector("#mic-button");
const partial = document.querySelector("#partial");
const clearButton = document.querySelector("#clear-button");
const stopAudio = document.querySelector("#stop-audio");
let csrf = "";
let socket = null;
let mediaStream = null;
let audioContext = null;
let processor = null;
let sourceNode = null;
let currentAudio = null;
let holding = false;
let voiceReadyResolve = null;

function setState(label, key = "idle") {
  stateLabel.textContent = label;
  stateLabel.dataset.state = key;
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

async function initialize() {
  try {
    const response = await fetch("/api/session", {credentials: "same-origin"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Session failed");
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
  addMessage("user", text);
  setState("Processing", "processing");
  input.disabled = true;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Butters-CSRF": csrf},
      body: JSON.stringify({text}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "Request failed");
    addMessage("assistant", data.response_text);
    setState("Speaking", "speaking");
    await speak(data.trace_id);
    setState("Ready", "idle");
  } catch (error) {
    addMessage("assistant", error.message || "The request failed safely.");
    setState("Error", "error");
  } finally {
    input.disabled = false;
    input.focus();
  }
}

async function speak(traceId) {
  try {
    const response = await fetch("/api/speech", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Butters-CSRF": csrf},
      body: JSON.stringify({trace_id: traceId}),
    });
    if (!response.ok) return;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    currentAudio = new Audio(url);
    stopAudio.hidden = false;
    await new Promise(resolve => {
      currentAudio.onended = resolve;
      currentAudio.onerror = resolve;
      currentAudio.play().catch(resolve);
    });
    URL.revokeObjectURL(url);
  } catch (_) {
    // Text response remains usable when local audio is not configured.
  } finally {
    stopAudio.hidden = true;
    currentAudio = null;
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendText(text);
});

clearButton.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/session/conversation", {
      method: "DELETE",
      credentials: "same-origin",
      headers: {"X-Butters-CSRF": csrf},
    });
    const data = await response.json();
    if (!response.ok) throw new Error();
    csrf = data.csrf_token;
    conversation.replaceChildren();
    addMessage("assistant", "Conversation cleared.");
  } catch (_) {
    setState("Could not clear", "error");
  }
});

stopAudio.addEventListener("click", () => {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.currentTime = 0;
  }
  stopAudio.hidden = true;
  setState("Ready", "idle");
});

async function beginVoice(event) {
  event.preventDefault();
  if (holding || !csrf) return;
  holding = true;
  micButton.classList.add("active");
  setState("Requesting microphone", "listening");
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}, video: false});
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    await audioContext.resume();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws/voice`);
    socket.binaryType = "arraybuffer";
    socket.onmessage = handleVoiceEvent;
    socket.onerror = () => setState("Voice connection error", "error");
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
    if (!holding) {
      cleanupVoice();
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
    holding = false;
    micButton.classList.remove("active");
    setState("Microphone unavailable", "error");
    cleanupVoice();
  }
}

async function endVoice(event) {
  if (event) event.preventDefault();
  if (!holding) return;
  holding = false;
  micButton.classList.remove("active");
  if (processor) processor.onaudioprocess = null;
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({type: "stop"}));
    setState("Transcribing", "processing");
  }
  if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
  if (sourceNode) sourceNode.disconnect();
  if (processor) processor.disconnect();
}

function handleVoiceEvent(event) {
  const data = JSON.parse(event.data);
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
    addMessage("assistant", data.response_text);
    setState("Speaking", "speaking");
    speak(data.trace_id).finally(() => setState("Ready", "idle"));
  }
  if (data.type === "error") {
    partial.hidden = true;
    addMessage("assistant", data.message || "Voice request failed safely.");
    setState("Voice error", "error");
  }
  if (["assistant", "error", "cancelled"].includes(data.type)) cleanupVoice();
}

function cleanupVoice() {
  if (processor) processor.disconnect();
  if (sourceNode) sourceNode.disconnect();
  if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
  if (audioContext) audioContext.close().catch(() => {});
  if (socket && socket.readyState === WebSocket.OPEN) socket.close();
  socket = mediaStream = audioContext = processor = sourceNode = null;
  voiceReadyResolve = null;
  holding = false;
  micButton.classList.remove("active");
}

micButton.addEventListener("pointerdown", beginVoice);
window.addEventListener("pointerup", endVoice);
micButton.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) beginVoice(event); });
micButton.addEventListener("keyup", event => { if (["Enter", " "].includes(event.key)) endVoice(event); });
window.addEventListener("beforeunload", cleanupVoice);
initialize();
