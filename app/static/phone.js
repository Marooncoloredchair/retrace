const FRAME_MS = 100;
const WIDTH = 320;
const HEIGHT = 240;

const auth = meta("retrace-auth");
const token = meta("retrace-token");
const badge = document.getElementById("badge");
const detail = document.getElementById("detail");
const errorEl = document.getElementById("error");
const video = document.getElementById("preview");
const cameraInfo = document.getElementById("cameraInfo");
const startBtn = document.getElementById("start");
const stopBtn = document.getElementById("stop");
const fpsEl = document.getElementById("fps");
const sentEl = document.getElementById("sent");
const droppedEl = document.getElementById("dropped");
const sizeEl = document.getElementById("size");
const socketEl = document.getElementById("socket");
const canvas = document.getElementById("frame");
const ctx = canvas.getContext("2d", { alpha: false });

let stream = null;
let socket = null;
let socketReady = false;
let streaming = false;
let loopId = 0;
let sequence = 0;
let sent = 0;
let dropped = 0;
let lastBytes = 0;
let lastSendAt = 0;
let reconnectAttempt = 0;
let reconnectTimer = null;
const sendTimes = [];

ctx.imageSmoothingEnabled = true;
ctx.imageSmoothingQuality = "high";

function meta(name) {
  const element = document.querySelector(`meta[name="${name}"]`);
  return element ? element.getAttribute("content") || "" : "";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function showError(message) {
  errorEl.hidden = false;
  errorEl.textContent = message;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

function setSocketLabel(label) {
  socketEl.textContent = label;
}

function renderStats() {
  const now = performance.now();
  while (sendTimes.length && now - sendTimes[0] > 1000) sendTimes.shift();
  fpsEl.textContent = streaming ? String(sendTimes.length) : "—";
  sentEl.textContent = String(sent);
  droppedEl.textContent = String(dropped);
  sizeEl.textContent = lastBytes ? `${Math.max(1, Math.round(lastBytes / 1024))} KB` : "—";

  badge.className = "badge";
  if (!streaming) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    badge.textContent = "Reconnecting";
    badge.classList.add("wait");
    return;
  }
  if (lastSendAt && now - lastSendAt < 1500) {
    badge.textContent = "Streaming is active";
    badge.classList.add("live");
    return;
  }
  badge.textContent = "Camera on, waiting to send";
  badge.classList.add("wait");
}

function cameraErrorMessage(error) {
  const name = error && error.name ? error.name : "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "Safari blocked the camera. Tap the AA button in the address bar, open Website Settings, and set Camera to Allow. You can also check Settings, Apps, Safari, Camera. Then tap Start Camera again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "The rear camera is not available on this device. ReTrace will not switch to the front camera.";
  }
  if (name === "NotReadableError") {
    return "The camera is busy. Close other apps that might be using it, then tap Start Camera again.";
  }
  if (name === "SecurityError" || !window.isSecureContext) {
    return "Safari only allows the camera on a secure page. Open the https link from the QR code in Safari itself.";
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    return "This browser cannot open the camera. On iPhone, use Safari and the https link from the QR code, not an in-app browser.";
  }
  return `Could not start the camera${name ? ` (${name})` : ""}. ${error && error.message ? error.message : ""}`.trim();
}

async function openRearCamera() {
  const attempts = [
    {
      audio: false,
      video: {
        facingMode: { exact: "environment" },
        width: { ideal: 640 },
        height: { ideal: 480 },
      },
    },
    {
      audio: false,
      video: { facingMode: "environment" },
    },
  ];
  let lastError = null;
  for (const constraints of attempts) {
    try {
      return await withTimeout(
        navigator.mediaDevices.getUserMedia(constraints),
        20000,
        "Safari did not respond to the camera request. Check the camera permission for this website, then try again."
      );
    } catch (error) {
      lastError = error;
      if (error.name === "NotAllowedError" || error.name === "PermissionDeniedError" || error.name === "SecurityError") {
        throw error;
      }
    }
  }
  throw lastError || new Error("Rear camera was not found.");
}

function withTimeout(promise, ms, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

function stopTracks() {
  if (!stream) return;
  for (const track of stream.getTracks()) track.stop();
  stream = null;
  video.pause();
  video.srcObject = null;
}

function closeSocket() {
  socketReady = false;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (!socket) return;
  const current = socket;
  socket = null;
  current.onopen = null;
  current.onclose = null;
  current.onerror = null;
  current.onmessage = null;
  try {
    current.close(1000, "stopped");
  } catch (_error) {
    /* already closing */
  }
}

function stopCamera(message) {
  streaming = false;
  loopId += 1;
  closeSocket();
  stopTracks();
  startBtn.disabled = auth !== "ok";
  stopBtn.disabled = true;
  badge.className = "badge";
  badge.textContent = "Camera off";
  detail.textContent = message || "Camera is off. Nothing is being sent.";
  setSocketLabel("closed");
  renderStats();
}

function scheduleReconnect() {
  if (!streaming || reconnectTimer) return;
  const delay = Math.min(5000, 400 * 2 ** reconnectAttempt);
  reconnectAttempt += 1;
  detail.textContent = `Connection lost. Retrying in ${(delay / 1000).toFixed(1)}s.`;
  renderStats();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (streaming) connectSocket();
  }, delay);
}

function connectSocket() {
  if (!streaming || !token) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/phone`;
  setSocketLabel("connecting");
  socketReady = false;
  const next = new WebSocket(url);
  next.binaryType = "arraybuffer";
  socket = next;

  next.onopen = () => {
    if (socket !== next) return;
    next.send(token);
    socketReady = true;
    reconnectAttempt = 0;
    setSocketLabel("open");
    detail.textContent = "Rear camera is on and the stream is connected.";
    renderStats();
  };

  next.onerror = () => {
    if (socket !== next) return;
    setSocketLabel("error");
  };

  next.onclose = (event) => {
    if (socket !== next) return;
    socket = null;
    setSocketLabel("closed");
    if (!streaming) return;
    if (event.code === 1008) {
      stopCamera("The server rejected this session.");
      showError("This QR code does not match the running server. Generate a new QR code without restarting if the server is still up. If you restarted the server, the old QR code no longer works.");
      return;
    }
    if (event.code === 4000) {
      stopCamera("This phone was disconnected.");
      showError("Another phone connected to this session, so this one stopped.");
      return;
    }
    scheduleReconnect();
  };
}

function waitForVideoFrame() {
  if (typeof video.requestVideoFrameCallback !== "function") {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(finish, 200);
    function finish() {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve();
    }
    video.requestVideoFrameCallback(() => finish());
  });
}

function drawFrame() {
  const srcW = video.videoWidth;
  const srcH = video.videoHeight;
  if (!srcW || !srcH) return false;
  const srcAspect = srcW / srcH;
  const destAspect = WIDTH / HEIGHT;
  let cropW;
  let cropH;
  let cropX;
  let cropY;
  if (srcAspect > destAspect) {
    cropH = srcH;
    cropW = srcH * destAspect;
    cropX = (srcW - cropW) / 2;
    cropY = 0;
  } else {
    cropW = srcW;
    cropH = srcW / destAspect;
    cropX = 0;
    cropY = (srcH - cropH) / 2;
  }
  ctx.drawImage(video, cropX, cropY, cropW, cropH, 0, 0, WIDTH, HEIGHT);
  return true;
}

function canvasToJpeg() {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (!blob) reject(new Error("Could not compress the camera frame."));
        else resolve(blob);
      },
      "image/jpeg",
      0.6
    );
  });
}

function writeUint64(view, offset, value) {
  const big = BigInt(value);
  if (typeof view.setBigUint64 === "function") {
    view.setBigUint64(offset, big);
    return;
  }
  view.setUint32(offset, Number(big >> 32n));
  view.setUint32(offset + 4, Number(big & 0xffffffffn));
}

async function sendFrame() {
  if (!socketReady || !socket || socket.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > 0) {
    dropped += 1;
    renderStats();
    return;
  }
  const capturedAt = Date.now();
  if (!drawFrame()) return;
  const blob = await canvasToJpeg();
  if (!streaming || !socket || socket.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > 0) {
    dropped += 1;
    renderStats();
    return;
  }
  const jpeg = new Uint8Array(await blob.arrayBuffer());
  const packet = new Uint8Array(12 + jpeg.length);
  const view = new DataView(packet.buffer);
  view.setUint32(0, sequence >>> 0);
  writeUint64(view, 4, capturedAt);
  packet.set(jpeg, 12);
  socket.send(packet);
  sequence = (sequence + 1) >>> 0;
  sent += 1;
  lastBytes = jpeg.length;
  lastSendAt = performance.now();
  sendTimes.push(lastSendAt);
  renderStats();
}

async function runLoop(id) {
  let nextSlot = performance.now();
  while (streaming && loopId === id) {
    nextSlot += FRAME_MS;
    await waitForVideoFrame();
    if (!streaming || loopId !== id) break;
    try {
      await sendFrame();
    } catch (error) {
      showError(error.message || "Could not send a frame.");
    }
    const delay = nextSlot - performance.now();
    if (delay > 0) await sleep(delay);
    else nextSlot = performance.now();
    renderStats();
  }
}

async function startCamera() {
  clearError();
  if (auth !== "ok" || !token) {
    showError("This page has no valid session. Scan a new QR code from the laptop.");
    return;
  }
  if (!window.isSecureContext) {
    showError("Safari blocked the camera because this page is not secure. Use the https link from the QR code.");
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showError("This browser has no camera API. Open the QR link in Safari.");
    return;
  }
  stopCamera();
  streaming = false;
  startBtn.disabled = true;
  badge.className = "badge wait";
  badge.textContent = "Starting camera";
  detail.textContent = "Asking Safari for the rear camera.";
  try {
    stream = await openRearCamera();
    video.srcObject = stream;
    video.muted = true;
    video.playsInline = true;
    await video.play();
    const track = stream.getVideoTracks()[0];
    const settings = track.getSettings ? track.getSettings() : {};
    const facing = settings.facingMode || "";
    if (facing && facing !== "environment") {
      stopTracks();
      startBtn.disabled = false;
      badge.className = "badge";
      badge.textContent = "Camera off";
      showError("The browser opened the front camera. ReTrace did not start streaming. Tap Start Camera to try the rear camera again.");
      return;
    }
    sent = 0;
    dropped = 0;
    sequence = 0;
    lastBytes = 0;
    lastSendAt = 0;
    sendTimes.length = 0;
    const reported = settings.width && settings.height ? `${settings.width}×${settings.height}` : "an unreported size";
    const lens = facing === "environment" ? "Rear camera" : "Camera";
    cameraInfo.textContent = `${lens} reports ${reported}. The stream sends a 320×240 center crop, up to 10 FPS. The numbers below are measured.`;
    track.onended = () => {
      if (!streaming) return;
      stopCamera("The camera stopped.");
      showError("The camera stopped. Safari does this if the phone locks, the tab goes to the background, or another app takes the camera. Tap Start Camera to resume.");
    };
    streaming = true;
    stopBtn.disabled = false;
    detail.textContent = "Rear camera is on. Connecting the stream.";
    connectSocket();
    loopId += 1;
    void runLoop(loopId);
  } catch (error) {
    stopTracks();
    streaming = false;
    startBtn.disabled = false;
    stopBtn.disabled = true;
    badge.className = "badge";
    badge.textContent = "Camera off";
    detail.textContent = "The camera did not start. Nothing is being sent.";
    showError(cameraErrorMessage(error));
  }
}

if (auth === "rejected") {
  startBtn.disabled = true;
  detail.textContent = "This link does not match the running server.";
  showError("This QR code does not match the running server. If ReTrace is still running, generate the QR code again. If you restarted it, the old code is no longer valid.");
} else if (auth !== "ok") {
  startBtn.disabled = true;
  detail.textContent = "Open the QR link from the laptop. Nothing is sent from this page.";
  showError("This page needs the link from the QR code. On the laptop, run scripts/make_qr.py with the tunnel address.");
}

startBtn.addEventListener("click", () => {
  unlockAudio();
  void startCamera();
});

stopBtn.addEventListener("click", () => {
  clearError();
  stopCamera("Camera is off. Nothing is being sent.");
});

window.addEventListener("offline", () => {
  if (!streaming) return;
  detail.textContent = "The network dropped. The stream will retry when it returns.";
});

window.addEventListener("online", () => {
  if (!streaming) return;
  detail.textContent = "Network is back. Reconnecting.";
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  connectSocket();
});

function markButtons(kind) {
  return document.querySelectorAll(`[data-mark="${kind}"]`);
}

function markLabel(eventName) {
  const base = String(eventName || "").replace(/_REPEAT$/, "");
  if (base === "START_TEST") return "Start test";
  if (base === "LEFT_SAVED_LOCATION") return "Left saved location";
  if (base === "ARRIVED_A") return "Arrived at A";
  if (base === "ARRIVED_B") return "Arrived at B";
  if (base === "ARRIVED_C") return "Arrived at C";
  if (base === "END_TEST") return "End test";
  return base;
}

function segmentLabel(value) {
  if (value === "A" || value === "B" || value === "C") return `arrived at ${value}`;
  if (value === "AWAY") return "away from the saved places";
  return "no location mark yet";
}

function renderPhoneMarks(report) {
  const decision = report && report.decision;
  const rawCall = document.getElementById("rawCall");
  const stableCall = document.getElementById("stableCall");
  const markFlash = document.getElementById("markFlash");
  const phoneTestState = document.getElementById("phoneTestState");
  if (rawCall) rawCall.textContent = decision ? decision.predicted : "—";
  if (stableCall) stableCall.textContent = decision ? decision.stable_predicted || "UNKNOWN" : "—";
  const failureCall = document.getElementById("failureCall");
  if (failureCall) {
    failureCall.textContent = decision && decision.failure_text
      ? decision.failure_text
      : "The laptop shows why each frame matched or stayed UNKNOWN.";
  }
  const testing = Boolean(report && report.testing);
  const segment = report ? report.segment : "";
  const allowed = auth === "ok";
  markButtons("start").forEach((button) => {
    button.disabled = !allowed || testing;
  });
  markButtons("stop").forEach((button) => {
    button.disabled = !allowed || !testing;
  });
  ["LEFT_SAVED_LOCATION", "ARRIVED_A", "ARRIVED_B", "ARRIVED_C"].forEach((kind) => {
    markButtons(kind).forEach((button) => {
      button.disabled = !allowed || !testing;
      const active = (kind === "LEFT_SAVED_LOCATION" && segment === "AWAY")
        || (kind === "ARRIVED_A" && segment === "A")
        || (kind === "ARRIVED_B" && segment === "B")
        || (kind === "ARRIVED_C" && segment === "C");
      button.classList.toggle("active", active);
    });
  });
  const lastMark = report && report.last_mark;
  if (markFlash && lastMark) {
    const label = markLabel(lastMark.event);
    markFlash.textContent = lastMark.repeat
      ? `${label} pressed again. The first press still holds the time. Extra presses: ${lastMark.extra_presses}.`
      : `${label} logged on the laptop. Press it again if you want. The first press keeps the time.`;
    markFlash.classList.add("fresh");
  }
  if (phoneTestState) {
    const archived = report && report.last_archive ? ` Previous log saved as ${report.last_archive}.` : "";
    phoneTestState.textContent = testing
      ? `Test running, ${segmentLabel(segment)}.${archived}`
      : `Not testing.${archived}`;
  }
}

async function phonePost(path) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    showError(payload.detail || "The mark was not saved.");
    return null;
  }
  clearError();
  return payload;
}

async function phoneMark(eventName) {
  const response = await fetch("/api/phone/test/event", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event: eventName }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    showError(payload.detail || "The mark was not saved.");
    return;
  }
  clearError();
  renderPhoneMarks(payload);
}

async function pollPhoneMarks() {
  if (auth !== "ok" || !token) return;
  try {
    const response = await fetch("/api/phone/places", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return;
    renderPhoneMarks(await response.json());
  } catch (_error) {
    /* The next poll retries. A missed status read does not stop the camera. */
  }
}

if (auth === "ok") {
  document.querySelectorAll("[data-mark]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.mark;
      if (kind === "start") {
        if (!streaming) void startCamera();
        void phonePost("/api/phone/test/start").then((payload) => {
          if (payload) renderPhoneMarks(payload);
        });
      } else if (kind === "stop") {
        void phonePost("/api/phone/test/stop").then((payload) => {
          if (payload) renderPhoneMarks(payload);
        });
      } else {
        void phoneMark(kind);
      }
    });
  });
  void pollPhoneMarks();
  setInterval(pollPhoneMarks, 500);
  bindGuidedExperiment();
} else {
  document.querySelectorAll("[data-mark]").forEach((button) => {
    button.disabled = true;
  });
  const guideStart = document.getElementById("guideStart");
  const guideStop = document.getElementById("guideStop");
  if (guideStart) guideStart.disabled = true;
  if (guideStop) guideStop.disabled = true;
}

let audioContext = null;
let lastSpeakId = 0;
let speechReady = false;
let lastPhaseSpoken = "";
let lastSpokenText = "";
let lastSpokenAt = 0;
let heldUtterance = null;
let cachedVoice = null;

function englishVoice() {
  const synth = window.speechSynthesis;
  if (!synth || typeof synth.getVoices !== "function") return null;
  const voices = synth.getVoices() || [];
  if (!voices.length) return cachedVoice;
  cachedVoice = voices.find((voice) => voice.lang === "en-US" && voice.localService)
    || voices.find((voice) => voice.lang && voice.lang.startsWith("en"))
    || cachedVoice;
  return cachedVoice;
}

function unlockSpeech() {
  const synth = window.speechSynthesis;
  if (!synth) return;
  englishVoice();
  try {
    synth.resume();
  } catch (_error) {
    /* The beep still marks a cue if speech cannot resume. */
  }
}

function unlockAudio() {
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  if (!audioContext) audioContext = new Context();
  if (audioContext.state === "suspended") void audioContext.resume();
  unlockSpeech();
}

function beep() {
  if (!audioContext) return;
  const oscillator = audioContext.createOscillator();
  const gain = audioContext.createGain();
  oscillator.frequency.value = 880;
  gain.gain.setValueAtTime(0.2, audioContext.currentTime);
  oscillator.connect(gain);
  gain.connect(audioContext.destination);
  oscillator.start();
  oscillator.stop(audioContext.currentTime + 0.12);
}

function speak(text) {
  const synth = window.speechSynthesis;
  if (!text || !synth || typeof SpeechSynthesisUtterance !== "function") {
    beep();
    return;
  }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-US";
  utterance.rate = 1;
  utterance.volume = 1;
  utterance.onerror = () => beep();
  // Safari drops the phrase if this object can be discarded, and it drops a
  // phrase that is spoken after a delay. Speak in this turn, with the default voice.
  heldUtterance = utterance;
  try {
    synth.resume();
    synth.speak(utterance);
  } catch (_error) {
    beep();
  }
}

function rememberSpeech(text) {
  lastSpokenText = text;
  lastSpokenAt = Date.now();
  speak(text);
}

function sayOnce(text) {
  if (!text) return;
  if (text === lastSpokenText && Date.now() - lastSpokenAt < 2500) return;
  rememberSpeech(text);
}

function maybeSpeak(report) {
  if (!report) return;
  if (!speechReady) {
    speechReady = true;
    lastSpeakId = report.speak_id || 0;
    if (!report.active) {
      lastPhaseSpoken = report.phase || "";
      return;
    }
  }
  if (report.speak_id && report.speak_id !== lastSpeakId) {
    lastSpeakId = report.speak_id;
    sayOnce(report.speak || "");
  }
  if (!report.active) return;
  const phase = report.phase || "";
  if (phase && phase !== lastPhaseSpoken) {
    lastPhaseSpoken = phase;
    sayOnce(report.instruction || "");
  }
}

function readWalkSeconds(id) {
  const value = Number(document.getElementById(id).value);
  if (!Number.isInteger(value) || value < 3 || value > 60) return null;
  return value;
}

function renderGuide(report) {
  const count = document.getElementById("guideCount");
  const stage = document.getElementById("guideStage");
  const instruction = document.getElementById("guideInstruction");
  const next = document.getElementById("guideNext");
  const score = document.getElementById("guideScore");
  const setup = document.getElementById("guideSetup");
  const startButton = document.getElementById("guideStart");
  const stopButton = document.getElementById("guideStop");
  const active = Boolean(report && report.active);
  if (count) {
    count.hidden = !active;
    count.textContent = active ? String(report.countdown) : "";
  }
  if (stage) {
    stage.textContent = active ? report.label || "Running" : report && report.status === "complete" ? "Complete" : report && report.status === "cancelled" ? "Stopped" : "Ready";
  }
  if (instruction) {
    instruction.textContent = active
      ? report.instruction
      : "Start the camera, take the ring position at the desk, then press Start test.";
  }
  if (next) next.textContent = active && report.next_instruction ? `Next: ${report.next_instruction}` : "";
  if (score) {
    score.textContent = active && report.predicted
      ? `This test: ${report.predicted}. Stable ${report.stable_predicted || "UNKNOWN"}. A inliers ${report.inliers_a}. Fresh references ${report.reference_count}.`
      : "";
  }
  if (setup) setup.hidden = active;
  if (active) {
    const existing = document.getElementById("error");
    if (existing && /did not start|lost the start/.test(existing.textContent || "")) clearError();
  }
  if (startButton) startButton.disabled = active || !streaming;
  if (stopButton) stopButton.disabled = !active;
  maybeSpeak(report);
}

async function pollGuide() {
  if (auth !== "ok" || !token) return null;
  try {
    const response = await fetch("/api/phone/experiment", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return null;
    const report = await response.json();
    renderGuide(report);
    return report;
  } catch (_error) {
    /* The next poll retries. */
    return null;
  }
}

function selectedViews() {
  const picked = document.querySelector('input[name="guideView"]:checked');
  if (picked && (picked.value === "single" || picked.value === "multi" || picked.value === "returns")) {
    return picked.value;
  }
  return "returns";
}

function bindGuidedExperiment() {
  const startButton = document.getElementById("guideStart");
  const stopButton = document.getElementById("guideStop");
  if (!startButton || !stopButton) return;
  startButton.disabled = !streaming;
  startButton.addEventListener("click", () => {
    const away = readWalkSeconds("awaySeconds");
    const back = readWalkSeconds("backSeconds");
    if (away === null || back === null) {
      showError("Walk away and walk back must each be a whole number from 3 to 60 seconds.");
      return;
    }
    unlockAudio();
    const opening = {
      single: "ReTrace experiment starting. Stand in your starting position. Five, four, three, two, one.",
      multi: "ReTrace experiment starting. Aim at the corner of your desk and keep it in view. Five, four, three, two, one.",
      returns: "ReTrace return trials starting. Stand at your desk and aim at the cabinet. The saved corner will not be changed. Five, four, three, two, one.",
    }[selectedViews()];
    beep();
    rememberSpeech(opening);
    if (!streaming) void startCamera();
    void (async () => {
      try {
        const response = await fetch("/api/phone/experiment/start", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            away_seconds: away,
            back_seconds: back,
            views: selectedViews(),
          }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          const report = await pollGuide();
          if (report && report.active) {
            clearError();
            return;
          }
          showError(payload.detail || "The experiment did not start.");
          return;
        }
        clearError();
        renderGuide(payload);
      } catch (_error) {
        const report = await pollGuide();
        if (report && report.active) clearError();
        else showError("The phone lost the start message. If the countdown is moving, the test is already running.");
      }
    })();
  });
  stopButton.addEventListener("click", () => {
    unlockAudio();
    void (async () => {
      const response = await fetch("/api/phone/experiment/stop", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: "{}",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        showError(payload.detail || "The experiment did not stop.");
        return;
      }
      clearError();
      renderGuide(payload);
    })();
  });
  void pollGuide();
  setInterval(pollGuide, 200);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !streaming) return;
  const track = stream && stream.getVideoTracks()[0];
  if (!track || track.readyState === "ended") {
    stopCamera("The camera stopped while Safari was in the background.");
    showError("The camera stopped while Safari was in the background. Tap Start Camera to resume.");
  }
});

window.addEventListener("pagehide", () => {
  stopCamera("Camera is off. Nothing is being sent.");
});

setInterval(renderStats, 250);
setInterval(() => {
  const synth = window.speechSynthesis;
  if (!synth || !synth.speaking) return;
  try {
    synth.pause();
    synth.resume();
  } catch (_error) {
    /* The next cue tries again. */
  }
}, 8000);
if (window.speechSynthesis && typeof window.speechSynthesis.addEventListener === "function") {
  window.speechSynthesis.addEventListener("voiceschanged", () => {
    englishVoice();
  });
}
englishVoice();
