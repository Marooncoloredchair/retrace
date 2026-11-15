const auth = meta("retrace-auth");
const token = meta("retrace-token");
const badge = document.getElementById("badge");
const stage = document.getElementById("stage");
const view = document.getElementById("view");
const viewCtx = view.getContext("2d", { alpha: false });
const placeholder = document.getElementById("placeholder");
const liveTag = document.getElementById("livetag");
const errorEl = document.getElementById("error");
const statusEl = document.getElementById("status");
const fpsEl = document.getElementById("fps");
const latencyEl = document.getElementById("latency");
const framesEl = document.getElementById("frames");
const serverFpsEl = document.getElementById("serverFps");
const skippedEl = document.getElementById("skipped");
const phoneEl = document.getElementById("phone");
const sizeEl = document.getElementById("size");

let socket = null;
let reconnectTimer = null;
let reconnectAttempt = 0;
let shown = 0;
let skipped = 0;
let lastSequence = -1;
let lastArrival = 0;
let lastLatency = null;
let lastWidth = 0;
let lastHeight = 0;
let lastBytes = 0;
let serverStatus = null;
let socketLabel = "connecting";
const arrivals = [];

function meta(name) {
  const element = document.querySelector(`meta[name="${name}"]`);
  return element ? element.getAttribute("content") || "" : "";
}

function showError(message) {
  errorEl.hidden = false;
  errorEl.textContent = message;
}

function readUint64(view, offset) {
  if (typeof view.getBigUint64 === "function") return Number(view.getBigUint64(offset));
  const high = view.getUint32(offset);
  const low = view.getUint32(offset + 4);
  return high * 2 ** 32 + low;
}

function scheduleReconnect() {
  if (reconnectTimer || auth !== "ok") return;
  const delay = Math.min(5000, 400 * 2 ** reconnectAttempt);
  reconnectAttempt += 1;
  socketLabel = "reconnecting";
  render();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function connect() {
  if (auth !== "ok" || !token) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/desktop`;
  socketLabel = "connecting";
  const next = new WebSocket(url);
  next.binaryType = "arraybuffer";
  socket = next;
  render();

  next.onopen = () => {
    if (socket !== next) return;
    next.send(token);
    reconnectAttempt = 0;
    socketLabel = "connected";
    render();
  };

  next.onerror = () => {
    if (socket !== next) return;
    socketLabel = "error";
    render();
  };

  next.onclose = (event) => {
    if (socket !== next) return;
    socket = null;
    socketLabel = "disconnected";
    if (event.code === 1008) {
      showError("The desktop session was rejected. Reload this page from http://127.0.0.1:8000/desktop on the laptop running ReTrace.");
      render();
      return;
    }
    scheduleReconnect();
  };

  next.onmessage = (event) => {
    if (socket !== next || typeof event.data === "string") return;
    void showFrame(event.data);
  };
}

async function showFrame(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 14 || bytes[12] !== 0xff || bytes[13] !== 0xd8) return;
  const header = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const sequence = header.getUint32(0);
  const capturedAt = readUint64(header, 4);
  const jpeg = bytes.subarray(12);
  let bitmap;
  try {
    bitmap = await createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }));
  } catch (_error) {
    return;
  }
  if (view.width !== bitmap.width || view.height !== bitmap.height) {
    view.width = bitmap.width;
    view.height = bitmap.height;
  }
  viewCtx.drawImage(bitmap, 0, 0);
  lastWidth = bitmap.width;
  lastHeight = bitmap.height;
  if (typeof bitmap.close === "function") bitmap.close();

  const now = performance.now();
  arrivals.push(now);
  while (arrivals.length && now - arrivals[0] > 1000) arrivals.shift();
  shown += 1;
  if (lastSequence >= 0 && sequence > lastSequence) {
    skipped += sequence - lastSequence - 1;
  }
  lastSequence = sequence;
  lastArrival = now;
  lastBytes = jpeg.length;
  const latency = Date.now() - capturedAt;
  lastLatency = Number.isFinite(latency) ? Math.round(latency) : null;
  placeholder.hidden = true;
  render();
}

function render() {
  const now = performance.now();
  while (arrivals.length && now - arrivals[0] > 1000) arrivals.shift();
  const recent = lastArrival && now - lastArrival < 1000;
  const phoneConnected = Boolean(serverStatus && serverStatus.phone_connected);
  const xiaoSource = Boolean(serverStatus && serverStatus.camera_source === "xiao_usb");
  const age = serverStatus ? serverStatus.last_frame_age_ms : null;
  const serverFresh = age !== null && age < 1500;
  const live = socketLabel === "connected" && Boolean(recent) && serverFresh;

  badge.className = "badge";
  stage.classList.toggle("stale", shown > 0 && !live);
  liveTag.hidden = shown === 0 || live;

  let status = "Connecting";
  if (auth !== "ok") status = "Not authorized";
  else if (live) status = "Live";
  else if (socketLabel === "reconnecting" || socketLabel === "disconnected") status = "Reconnecting";
  else if (shown > 0 && !phoneConnected && !xiaoSource) status = "Phone disconnected";
  else if ((phoneConnected || xiaoSource) && !recent) status = "Waiting for frames";
  else if (socketLabel === "connected") status = "Connected";
  else status = socketLabel.charAt(0).toUpperCase() + socketLabel.slice(1);

  if (live) {
    badge.textContent = "Streaming is active";
    badge.classList.add("live");
  } else if (status === "Phone disconnected" || status === "Reconnecting") {
    badge.textContent = status;
    badge.classList.add("wait");
  } else {
    badge.textContent = status;
  }

  statusEl.textContent = status;
  fpsEl.textContent = recent ? String(arrivals.length) : shown ? "0" : "—";
  latencyEl.textContent = lastLatency === null ? "n/a" : `${lastLatency} ms`;
  framesEl.textContent = String(shown);
  serverFpsEl.textContent = serverStatus ? String(serverStatus.receive_fps) : "—";
  skippedEl.textContent = String(skipped);
  if (!serverStatus) phoneEl.textContent = "—";
  else if (xiaoSource) phoneEl.textContent = serverStatus.xiao_connected ? "XIAO" : "XIAO off";
  else phoneEl.textContent = phoneConnected ? "Connected" : "Disconnected";
  sizeEl.textContent = lastWidth ? `${lastWidth}×${lastHeight}` : "—";
  renderCameraSource();
}

function renderCameraSource() {
  const fps = document.getElementById("cameraFps");
  const processing = document.getElementById("processingFps");
  const dropped = document.getElementById("cameraDropped");
  const recognition = document.getElementById("cameraRecognition");
  const detail = document.getElementById("cameraDetail");
  if (!fps || !serverStatus) return;
  const xiao = serverStatus.camera_source === "xiao_usb";
  fps.textContent = xiao ? String(serverStatus.camera_fps ?? "—") : "—";
  processing.textContent = serverStatus.processing_fps == null ? "—" : String(serverStatus.processing_fps);
  dropped.textContent = xiao ? String(serverStatus.frames_dropped ?? 0) : "0";
  const name = serverStatus.recognition || "—";
  const stable = serverStatus.recognition_stable || "";
  const features = serverStatus.orb_features;
  recognition.textContent = features == null ? name : `${name} · ${features} features`;
  const sensor = serverStatus.sensor || "";
  const problem = serverStatus.xiao_error || "";
  detail.textContent = problem || (xiao ? `${sensor}${stable ? ` Stable ${stable}.` : ""}` : "iPhone camera. The XIAO port is closed.");
  document.querySelectorAll('input[name="cameraSource"]').forEach((input) => {
    input.checked = input.value === (serverStatus.camera_source || "iphone");
  });
}

async function selectCameraSource(source) {
  if (!token) return;
  try {
    const response = await fetch("/api/camera", {
      method: "POST",
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ source }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      showError(payload.detail || "The camera source did not change.");
      await pollStatus();
      return;
    }
    errorEl.hidden = true;
    serverStatus = payload;
    placeholder.textContent = source === "xiao_usb" ? "Waiting for the XIAO camera." : "Waiting for the iPhone camera.";
  } catch (_error) {
    showError("The camera source did not change.");
  } finally {
    render();
  }
}

document.querySelectorAll('input[name="cameraSource"]').forEach((input) => {
  input.addEventListener("change", () => {
    if (input.checked) void selectCameraSource(input.value);
  });
});

async function pollStatus() {
  if (!token) return;
  try {
    const response = await fetch("/api/status", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) {
      serverStatus = null;
      return;
    }
    serverStatus = await response.json();
  } catch (_error) {
    serverStatus = null;
  } finally {
    render();
  }
}

if (auth !== "ok") {
  socketLabel = "unauthorized";
  placeholder.textContent = "Open http://127.0.0.1:8000/desktop in a browser on the laptop that is running ReTrace.";
  showError("This page does not have the desktop session. The tunnel link used by the phone cannot open the viewer.");
  render();
} else {
  connect();
  void pollStatus();
  setInterval(pollStatus, 1000);
  setInterval(render, 250);
}

const featureStage = document.getElementById("featureStage");
const featureView = document.getElementById("features");
const featureCtx = featureView.getContext("2d", { alpha: false });
const featurePlaceholder = document.getElementById("featurePlaceholder");
const orbCountEl = document.getElementById("orbCount");
const orbResponseEl = document.getElementById("orbResponse");
const blurScoreEl = document.getElementById("blurScore");
const analysisFpsEl = document.getElementById("analysisFps");
const processMsEl = document.getElementById("processMs");
const loggedRowsEl = document.getElementById("loggedRows");
const recordStart = document.getElementById("recordStart");
const recordStop = document.getElementById("recordStop");
const downloadCsvButton = document.getElementById("downloadCsv");
const recordState = document.getElementById("recordState");
const conditionInputs = document.querySelectorAll('input[name="condition"]');

let analysisSocket = null;
let analysisReconnectTimer = null;
let analysisAttempt = 0;
let analysisReport = null;

function selectedCondition() {
  const chosen = document.querySelector('input[name="condition"]:checked');
  return chosen ? chosen.value : "palm";
}

function conditionLabel(value) {
  return value === "outward" ? "outward-facing" : "palm-facing";
}

function renderAnalysis() {
  const metrics = analysisReport && analysisReport.metrics;
  const fresh = Boolean(metrics) && analysisReport.analysis_fps > 0;
  if (featureStage) featureStage.classList.toggle("stale", Boolean(metrics) && !fresh);
  orbCountEl.textContent = metrics ? String(metrics.feature_count) : "—";
  orbResponseEl.textContent = metrics ? `${metrics.median_response} (max ${metrics.max_response})` : "—";
  blurScoreEl.textContent = metrics ? String(metrics.blur_score) : "—";
  analysisFpsEl.textContent = analysisReport ? String(analysisReport.analysis_fps) : "—";
  processMsEl.textContent = metrics ? `${metrics.process_ms} ms` : "—";
  loggedRowsEl.textContent = analysisReport ? String(analysisReport.recorded_rows) : "0";
  const recording = Boolean(analysisReport && analysisReport.recording);
  recordStart.disabled = recording;
  recordStop.disabled = !recording;
  conditionInputs.forEach((input) => {
    input.disabled = recording;
  });
  if (!analysisReport) {
    recordState.textContent = "Not recording.";
    return;
  }
  const label = conditionLabel(analysisReport.condition);
  recordState.textContent = recording
    ? `Recording ${label}. ${analysisReport.recorded_rows} rows so far.`
    : `Not recording. ${analysisReport.recorded_rows} rows saved in this session.`;
}

async function showAnnotated(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 14 || bytes[12] !== 0xff || bytes[13] !== 0xd8) return;
  const jpeg = bytes.subarray(12);
  let bitmap;
  try {
    bitmap = await createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }));
  } catch (_error) {
    return;
  }
  if (featureView.width !== bitmap.width || featureView.height !== bitmap.height) {
    featureView.width = bitmap.width;
    featureView.height = bitmap.height;
  }
  featureCtx.drawImage(bitmap, 0, 0);
  if (typeof bitmap.close === "function") bitmap.close();
  featurePlaceholder.hidden = true;
}

function scheduleAnalysisReconnect() {
  if (analysisReconnectTimer || auth !== "ok") return;
  const delay = Math.min(5000, 400 * 2 ** analysisAttempt);
  analysisAttempt += 1;
  analysisReconnectTimer = setTimeout(() => {
    analysisReconnectTimer = null;
    connectAnalysis();
  }, delay);
}

function connectAnalysis() {
  if (auth !== "ok" || !token || !featureView) return;
  if (analysisSocket && (analysisSocket.readyState === WebSocket.OPEN || analysisSocket.readyState === WebSocket.CONNECTING)) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const next = new WebSocket(`${protocol}//${location.host}/ws/analysis`);
  next.binaryType = "arraybuffer";
  analysisSocket = next;
  next.onopen = () => {
    if (analysisSocket !== next) return;
    next.send(token);
    analysisAttempt = 0;
  };
  next.onmessage = (event) => {
    if (analysisSocket !== next || typeof event.data === "string") return;
    void showAnnotated(event.data);
  };
  next.onclose = (event) => {
    if (analysisSocket !== next) return;
    analysisSocket = null;
    if (event.code === 1008) return;
    scheduleAnalysisReconnect();
  };
}

async function pollAnalysis() {
  if (!token) return;
  try {
    const response = await fetch("/api/analysis", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return;
    analysisReport = await response.json();
  } catch (_error) {
    analysisReport = null;
  } finally {
    renderAnalysis();
  }
}

async function startRecording() {
  const response = await fetch("/api/recording/start", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ condition: selectedCondition() }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    showError(body.detail || "Could not start recording.");
    return;
  }
  clearError();
  await pollAnalysis();
}

async function stopRecording() {
  const response = await fetch("/api/recording/stop", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    showError("Could not stop recording.");
    return;
  }
  clearError();
  await pollAnalysis();
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

async function downloadCsv() {
  const response = await fetch("/api/recording.csv", {
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    showError("Could not download the CSV.");
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "retrace-feasibility.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

if (auth !== "ok" && recordStart) {
  recordStart.disabled = true;
  recordStop.disabled = true;
  downloadCsvButton.disabled = true;
  conditionInputs.forEach((input) => {
    input.disabled = true;
  });
}

if (auth === "ok" && featureView) {
  connectAnalysis();
  void pollAnalysis();
  setInterval(pollAnalysis, 500);
  recordStart.addEventListener("click", () => {
    void startRecording();
  });
  recordStop.addEventListener("click", () => {
    void stopRecording();
  });
  downloadCsvButton.addEventListener("click", () => {
    void downloadCsv();
  });
}

window.addEventListener("online", () => {
  if (auth === "ok") {
    connect();
    connectAnalysis();
    connectPlaces();
  }
});

const placeStage = document.getElementById("placeStage");
const placeView = document.getElementById("placeView");
const placeCtx = placeView ? placeView.getContext("2d", { alpha: false }) : null;
const placePlaceholder = document.getElementById("placePlaceholder");
const placeCall = document.getElementById("placeCall");
const placeDetail = document.getElementById("placeDetail");
const saveReferenceButton = document.getElementById("saveReference");
const clearPlaceButton = document.getElementById("clearPlace");
const downloadPlacesButton = document.getElementById("downloadPlaces");
const clearTestsButton = document.getElementById("clearTests");
const saveImagesInput = document.getElementById("saveImages");
const maskEnabledInput = document.getElementById("maskEnabled");
const clearMaskButton = document.getElementById("clearMask");
const referenceCounts = document.getElementById("referenceCounts");
const testState = document.getElementById("testState");
const arrivalLine = document.getElementById("arrivalLine");
const scoreLine = document.getElementById("scoreLine");
const confusionBody = document.getElementById("confusionBody");

let placeSocket = null;
let placeReconnectTimer = null;
let placeAttempt = 0;
let placeReport = null;
let placeBitmap = null;
let maskDrag = null;
let maskRect = null;

function selectedValue(name, fallback) {
  const chosen = document.querySelector(`input[name="${name}"]:checked`);
  return chosen ? chosen.value : fallback;
}

function segmentLabel(value) {
  if (value === "A" || value === "B" || value === "C") return `arrived at ${value}`;
  if (value === "AWAY") return "away from the saved places";
  return "no location mark yet";
}

function seconds(ms) {
  return `${(ms / 1000).toFixed(2)} s`;
}

function ratioText(part, whole, percent) {
  if (!whole) return "no frames in this window yet";
  return `${part}/${whole} (${percent}%)`;
}

function markButtons(kind) {
  return document.querySelectorAll(`[data-mark="${kind}"]`);
}

function markLabel(eventName) {
  const base = String(eventName || "").replace(/_REPEAT$/, "");
  if (base === "START_TEST") return "Start test";
  if (base === "START_DIAGNOSTIC") return "Start recording";
  if (base === "END_DIAGNOSTIC") return "Stop recording";
  if (base === "LEFT_SAVED_LOCATION") return "Left saved location";
  if (base === "ARRIVED_A") return "Arrived at A";
  if (base === "ARRIVED_B") return "Arrived at B";
  if (base === "ARRIVED_C") return "Arrived at C";
  if (base === "END_TEST") return "End test";
  return base;
}

function arrivalText(arrival) {
  if (!arrival) return "No arrival marks yet.";
  const lines = [];
  if (!(arrival.visits || []).length) lines.push("No arrival marks yet.");
  (arrival.visits || []).forEach((visit, index) => {
    const rawDelay = visit.raw_delay_ms === null || visit.raw_delay_ms === undefined
      ? "no raw name while this mark was active"
      : seconds(visit.raw_delay_ms);
    const stableDelay = visit.stable_delay_ms === null || visit.stable_delay_ms === undefined
      ? "no stable name while this mark was active"
      : seconds(visit.stable_delay_ms);
    const windowLead = visit.window_complete
      ? "Next 10 s"
      : `So far ${seconds(visit.window_ms)} of the 10 s window`;
    const drops = visit.stable_dropouts === 1 ? "1 time" : `${visit.stable_dropouts} times`;
    lines.push(
      `Arrival ${index + 1}, place ${visit.place}: first raw name ${rawDelay}; first stable name ${stableDelay}. ${windowLead}: raw ${ratioText(visit.raw_correct, visit.window_frames, visit.raw_percent)}, stable ${ratioText(visit.stable_correct, visit.window_frames, visit.stable_percent)}. Stable name dropped ${drops} across ${seconds(visit.segment_ms)} at this mark.`,
    );
  });
  if (!arrival.away_marks) {
    lines.push("No Left saved location mark yet.");
  } else {
    lines.push(
      `While marked away: ${arrival.away_frames} frames, ${arrival.away_raw_incorrect} raw place names, ${arrival.away_stable_incorrect} stable place names.`,
    );
  }
  return lines.join(" ");
}

async function placePost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : "{}",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    showError(payload.detail || "The place request failed.");
    return null;
  }
  clearError();
  return payload;
}

async function downloadPlaces() {
  const response = await fetch("/api/places/results.csv", {
    cache: "no-store",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    showError("Could not download the place CSV.");
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "retrace-places.csv";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function drawMask() {
  if (!placeCtx || !maskRect || !maskEnabledInput || !maskEnabledInput.checked) return;
  const frameW = placeView.width > 320 ? placeView.width / 2 : placeView.width;
  const height = placeView.height;
  placeCtx.strokeStyle = "#f3e2b0";
  placeCtx.lineWidth = 2;
  placeCtx.strokeRect(maskRect.x * frameW, maskRect.y * height, maskRect.w * frameW, maskRect.h * height);
}

function redrawPlace() {
  if (!placeCtx || !placeBitmap) return;
  placeCtx.drawImage(placeBitmap, 0, 0);
  drawMask();
}

const STAGE_TEXT = {
  zero_features: "zero features",
  too_few_matches: "too few descriptor matches",
  failed_geometry: "geometric check failed",
  too_few_inliers: "too few inliers",
  enough_inliers: "12 or more inliers",
  named: "named",
  margin: "places too close",
  no_references: "no saved reference",
};

function localStamp(ms) {
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function clockText(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

let thumbKey = "";

async function renderThumbs(items) {
  const strip = document.getElementById("referenceStrip");
  if (!strip) return;
  const photos = (items || []).filter((item) => item.photo);
  const key = photos.map((item) => item.id).join(",");
  if (key === thumbKey) return;
  thumbKey = key;
  strip.replaceChildren();
  for (const item of photos) {
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    image.alt = `Saved reference for place ${item.place}`;
    const caption = document.createElement("figcaption");
    caption.textContent = `${item.place} · ${item.feature_count} features`;
    figure.append(image, caption);
    strip.appendChild(figure);
    try {
      const response = await fetch(`/api/places/reference/${item.id}`, {
        cache: "no-store",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) continue;
      image.src = URL.createObjectURL(await response.blob());
    } catch (_error) {
      caption.textContent = `${item.place} · photo unavailable`;
    }
  }
}

function renderPlaces() {
  const decision = placeReport && placeReport.decision;
  const summary = placeReport && placeReport.summary;
  const rawCall = document.getElementById("rawCall");
  const stableCall = document.getElementById("stableCall");
  const failureLine = document.getElementById("failureLine");
  const placeLines = document.getElementById("placeLines");
  if (decision) {
    if (rawCall) rawCall.textContent = decision.predicted;
    if (stableCall) stableCall.textContent = decision.stable_predicted || "UNKNOWN";
    if (failureLine) {
      failureLine.textContent = decision.failure_text || decision.failure || "No decision yet";
      failureLine.classList.toggle("named", decision.failure === "named");
      failureLine.classList.toggle("bad", decision.failure && decision.failure !== "named");
    }
    const byPlace = decision.by_place || {};
    if (placeLines) {
      placeLines.textContent = ["A", "B", "C"].map((place) => {
        const item = byPlace[place] || {};
        if (item.stage === "no_references" || !item.reference_id) {
          return `${place}: no saved reference`;
        }
        const stage = STAGE_TEXT[item.stage] || item.stage || "no comparison";
        return `${place}: ${item.inliers} inliers, ${item.matches} descriptor matches, ${stage}`;
      }).join(" · ");
    }
    const counts = ["A", "B", "C"]
      .map((place) => `${place} ${decision.inliers[place]} inliers from ${decision.matches[place]} descriptor matches`)
      .join("; ");
    const reference = decision.best_reference_id ? ` Best reference ${decision.best_reference_id}.` : "";
    placeDetail.textContent = `${counts}.${reference} Match time ${decision.match_ms} ms.`;
    document.getElementById("inliersA").textContent = String(decision.inliers.A);
    document.getElementById("inliersB").textContent = String(decision.inliers.B);
    document.getElementById("inliersC").textContent = String(decision.inliers.C);
    document.getElementById("inlierTriple").textContent = `${decision.inliers.A} / ${decision.inliers.B} / ${decision.inliers.C}`;
    document.getElementById("matchCounts").textContent = `${decision.matches.A} / ${decision.matches.B} / ${decision.matches.C}`;
    document.getElementById("matchMs").textContent = `${decision.match_ms} ms`;
    document.getElementById("blurLive").textContent = decision.blur_score === "" || decision.blur_score === undefined ? "—" : String(decision.blur_score);
    document.getElementById("featureLive").textContent = decision.feature_count === "" || decision.feature_count === undefined ? "—" : String(decision.feature_count);
  } else if (placeReport) {
    const counts = placeReport.references || {};
    const saved = (counts.A || 0) + (counts.B || 0) + (counts.C || 0);
    if (rawCall) rawCall.textContent = "—";
    if (stableCall) stableCall.textContent = "—";
    if (failureLine) {
      failureLine.textContent = saved
        ? "Waiting for a camera frame."
        : "No saved references yet.";
      failureLine.classList.remove("named", "bad");
    }
    if (placeLines) placeLines.textContent = "";
    placeDetail.textContent = saved
      ? "The saved places are loaded. The comparison appears when a frame arrives."
      : "Save a few frames of place A. Until then there is nothing to match.";
  }
  const clock = placeReport ? placeReport.clock_delta_ms : null;
  document.getElementById("placeClock").textContent = clock === null || clock === undefined ? "—" : `${clock} ms`;
  if (placeReport && placeReport.references) {
    const counts = placeReport.references;
    const audit = placeReport.audit || {};
    const photoNote = audit.with_photo
      ? `${audit.with_photo} have a photo. ${audit.descriptors_only || 0} are descriptors only, so those have no picture on the right.`
      : "None of the saved references have a photo yet. Save this frame as A keeps the picture.";
    referenceCounts.textContent = `Saved references: A ${counts.A}, B ${counts.B}, C ${counts.C}. ${photoNote}`;
    const auditLine = document.getElementById("referenceAudit");
    if (auditLine) {
      if (!audit.items) {
        auditLine.textContent = "Checking saved references.";
      } else if (audit.intact) {
        auditLine.textContent = "The loaded references match the index. Nothing was rewritten.";
      } else {
        const problems = audit.problems || [];
        const shown = problems.slice(0, 4).join(" ");
        const extra = problems.length > 4 ? ` ${problems.length - 4} more.` : "";
        auditLine.textContent = `Check the saved references. ${shown}${extra}`;
      }
    }
    void renderThumbs(audit.items || []);
  }
  const sessionLine = document.getElementById("sessionLine");
  const session = placeReport && placeReport.session;
  if (sessionLine) {
    sessionLine.textContent = session
      ? `This log runs from ${clockText(session.start_unix_ms)} to ${clockText(session.end_unix_ms)}, ${session.frames} frames.`
      : "No recording in this log yet.";
  }
  const intervalState = document.getElementById("intervalState");
  if (intervalState && summary && summary.intervals) {
    const notes = summary.intervals;
    const labeled = notes.same_spot + notes.arm_only + notes.returned;
    if (labeled) {
      intervalState.textContent = `Labeled frames: same spot ${notes.same_spot}, arm only ${notes.arm_only}, returned ${notes.returned}.`;
    }
  }
  const testing = Boolean(placeReport && placeReport.testing);
  const segment = placeReport ? placeReport.segment : "";
  markButtons("start").forEach((button) => {
    button.disabled = testing || auth !== "ok";
  });
  markButtons("stop").forEach((button) => {
    button.disabled = !testing || auth !== "ok";
  });
  ["LEFT_SAVED_LOCATION", "ARRIVED_A", "ARRIVED_B", "ARRIVED_C"].forEach((kind) => {
    markButtons(kind).forEach((button) => {
      button.disabled = !testing || auth !== "ok";
      const active = (kind === "LEFT_SAVED_LOCATION" && segment === "AWAY")
        || (kind === "ARRIVED_A" && segment === "A")
        || (kind === "ARRIVED_B" && segment === "B")
        || (kind === "ARRIVED_C" && segment === "C");
      button.classList.toggle("active", active);
    });
  });
  const markFlash = document.getElementById("markFlash");
  const lastMark = placeReport && placeReport.last_mark;
  if (markFlash && lastMark) {
    const label = markLabel(lastMark.event);
    markFlash.textContent = lastMark.repeat
      ? `${label} pressed again. The first press still holds the time. Extra presses: ${lastMark.extra_presses}.`
      : `${label} logged. Press it again if you want. The first press keeps the time.`;
    markFlash.classList.add("fresh");
  }
  if (clearTestsButton) clearTestsButton.disabled = testing || auth !== "ok";
  if (saveReferenceButton) saveReferenceButton.disabled = testing || auth !== "ok";
  if (clearPlaceButton) clearPlaceButton.disabled = testing || auth !== "ok";
  const saveAButton = document.getElementById("saveA");
  const clearAButton = document.getElementById("clearA");
  const diagnosticStart = document.getElementById("diagnosticStart");
  const diagnosticStop = document.getElementById("diagnosticStop");
  const applyIntervals = document.getElementById("applyIntervals");
  if (saveAButton) saveAButton.disabled = testing || auth !== "ok";
  if (clearAButton) clearAButton.disabled = testing || auth !== "ok";
  if (diagnosticStart) diagnosticStart.disabled = testing || auth !== "ok";
  if (diagnosticStop) diagnosticStop.disabled = !testing || auth !== "ok";
  if (applyIntervals) applyIntervals.disabled = testing || auth !== "ok";
  const diagnosticState = document.getElementById("diagnosticState");
  if (diagnosticState && placeReport) {
    const shots = `${placeReport.unknown_shots || 0} of ${placeReport.unknown_shot_limit || 8} UNKNOWN screenshots saved`;
    const failures = summary && summary.failures;
    const failureNote = failures && Object.values(failures).some((count) => count)
      ? ` Zero features ${failures.zero_features}, too few matches ${failures.too_few_matches}, geometric check failed ${failures.failed_geometry}, too few inliers ${failures.too_few_inliers}, places too close ${failures.margin}, named ${failures.named}.`
      : "";
    const archived = placeReport.last_archive ? ` Previous log saved as ${placeReport.last_archive}.` : "";
    diagnosticState.textContent = placeReport.diagnostic
      ? `Recording place A. ${summary ? summary.test_frames : 0} frames. ${shots}.${failureNote}${archived}`
      : testing
        ? `A walk test is running. ${summary ? summary.test_frames : 0} frames.${archived}`
        : `Not recording. ${summary ? summary.test_frames : 0} frames in this log. ${shots}.${failureNote}${archived}`;
  }
  document.querySelectorAll('input[name="placeCamera"], input[name="place"]').forEach((input) => {
    input.disabled = testing || auth !== "ok";
  });
  if (testState) {
    const archived = placeReport && placeReport.last_archive ? ` Previous log saved as ${placeReport.last_archive}.` : "";
    testState.textContent = testing
      ? `Test running, ${segmentLabel(placeReport.segment)}, ${conditionLabel(placeReport.camera)}. ${summary ? summary.test_frames : 0} logged frames.${archived}`
      : `Not testing. ${summary ? summary.test_frames : 0} logged frames in this file.${archived}`;
  }
  if (arrivalLine) arrivalLine.textContent = arrivalText(placeReport && placeReport.arrival);
  if (!summary || !confusionBody) return;
  const truths = ["A", "B", "C", "AWAY", "UNFAMILIAR", "HAND"];
  const columns = ["A", "B", "C", "UNKNOWN"];
  confusionBody.replaceChildren();
  truths.forEach((truth) => {
    const row = document.createElement("tr");
    const label = document.createElement("th");
    label.textContent = truth === "UNFAMILIAR" ? "Unfamiliar" : truth === "HAND" ? "Hand only" : truth === "AWAY" ? "Away" : truth;
    row.appendChild(label);
    columns.forEach((column) => {
      const cell = document.createElement("td");
      const rowCounts = summary.confusion[truth] || {};
      cell.textContent = String(rowCounts[column] || 0);
      row.appendChild(cell);
    });
    confusionBody.appendChild(row);
  });
  const palm = summary.by_camera.palm;
  const outward = summary.by_camera.outward;
  scoreLine.textContent = `${summary.test_frames} test frames: ${summary.correct} correct place names, ${summary.incorrect} wrong place names, ${summary.unknown} UNKNOWN. Hand-only frames named as a place: ${summary.hand_named_as_place}. Unfamiliar frames named as a place: ${summary.unfamiliar_named_as_place}. Known places left UNKNOWN: ${summary.known_place_left_unknown}. Palm-facing ${palm.correct} correct, ${palm.incorrect} wrong, ${palm.unknown} UNKNOWN of ${palm.frames}. Outward-facing ${outward.correct} correct, ${outward.incorrect} wrong, ${outward.unknown} UNKNOWN of ${outward.frames}.`;
}

async function showPlaceFrame(buffer) {
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 14 || bytes[12] !== 0xff || bytes[13] !== 0xd8) return;
  const jpeg = bytes.subarray(12);
  let bitmap;
  try {
    bitmap = await createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }));
  } catch (_error) {
    return;
  }
  if (placeView.width !== bitmap.width || placeView.height !== bitmap.height) {
    placeView.width = bitmap.width;
    placeView.height = bitmap.height;
  }
  if (placeBitmap && typeof placeBitmap.close === "function") placeBitmap.close();
  placeBitmap = bitmap;
  redrawPlace();
  placePlaceholder.hidden = true;
}

function schedulePlaceReconnect() {
  if (placeReconnectTimer || auth !== "ok") return;
  const delay = Math.min(5000, 400 * 2 ** placeAttempt);
  placeAttempt += 1;
  placeReconnectTimer = setTimeout(() => {
    placeReconnectTimer = null;
    connectPlaces();
  }, delay);
}

function connectPlaces() {
  if (auth !== "ok" || !token || !placeView) return;
  if (placeSocket && (placeSocket.readyState === WebSocket.OPEN || placeSocket.readyState === WebSocket.CONNECTING)) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const next = new WebSocket(`${protocol}//${location.host}/ws/places`);
  next.binaryType = "arraybuffer";
  placeSocket = next;
  next.onopen = () => {
    if (placeSocket !== next) return;
    next.send(token);
    placeAttempt = 0;
  };
  next.onmessage = (event) => {
    if (placeSocket !== next || typeof event.data === "string") return;
    void showPlaceFrame(event.data);
  };
  next.onclose = (event) => {
    if (placeSocket !== next) return;
    placeSocket = null;
    if (event.code === 1008) return;
    schedulePlaceReconnect();
  };
}

async function pollPlaces() {
  if (!token) return;
  try {
    const response = await fetch("/api/places", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return;
    placeReport = await response.json();
  } catch (_error) {
    placeReport = null;
  } finally {
    renderPlaces();
  }
}

function canvasPoint(event) {
  const bounds = placeView.getBoundingClientRect();
  const relative = (event.clientX - bounds.left) / bounds.width;
  const scale = placeView.width > 320 ? placeView.width / 320 : 1;
  const x = Math.min(1, Math.max(0, relative * scale));
  const y = Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height));
  return { x, y };
}

async function sendMask(enabled) {
  const rect = maskRect || { x: 0, y: 0, w: 0, h: 0 };
  const payload = await placePost("/api/places/mask", {
    enabled: Boolean(enabled && rect.w >= 0.02 && rect.h >= 0.02),
    x: rect.x,
    y: rect.y,
    w: rect.w,
    h: rect.h,
  });
  if (payload) placeReport = payload;
  renderPlaces();
}

if (placeView) {
  placeView.addEventListener("pointerdown", (event) => {
    if (!maskEnabledInput.checked) return;
    maskDrag = canvasPoint(event);
    placeView.setPointerCapture(event.pointerId);
  });
  placeView.addEventListener("pointermove", (event) => {
    if (!maskDrag) return;
    const point = canvasPoint(event);
    const x = Math.min(maskDrag.x, point.x);
    const y = Math.min(maskDrag.y, point.y);
    maskRect = { x, y, w: Math.abs(point.x - maskDrag.x), h: Math.abs(point.y - maskDrag.y) };
    redrawPlace();
  });
  placeView.addEventListener("pointerup", () => {
    if (!maskDrag) return;
    maskDrag = null;
    void sendMask(true);
  });
}

if (auth !== "ok" && saveReferenceButton) {
  saveReferenceButton.disabled = true;
  clearPlaceButton.disabled = true;
  document.querySelectorAll("[data-mark]").forEach((button) => {
    button.disabled = true;
  });
  downloadPlacesButton.disabled = true;
  clearTestsButton.disabled = true;
  ["saveA", "clearA", "diagnosticStart", "diagnosticStop", "applyIntervals"].forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = true;
  });
}

if (auth === "ok" && placeView) {
  connectPlaces();
  void pollPlaces();
  setInterval(pollPlaces, 500);
  placeStage.classList.toggle("masking", maskEnabledInput.checked);
  maskEnabledInput.addEventListener("change", () => {
    placeStage.classList.toggle("masking", maskEnabledInput.checked);
    void sendMask(maskEnabledInput.checked);
  });
  clearMaskButton.addEventListener("click", () => {
    maskRect = null;
    maskEnabledInput.checked = false;
    placeStage.classList.remove("masking");
    void sendMask(false);
  });
  saveReferenceButton.addEventListener("click", () => {
    void (async () => {
      const payload = await placePost("/api/places/reference", {
        place: selectedValue("place", "A"),
        camera: selectedValue("placeCamera", "palm"),
        save_images: saveImagesInput.checked,
      });
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  clearPlaceButton.addEventListener("click", () => {
    void (async () => {
      const payload = await placePost("/api/places/clear", { place: selectedValue("place", "A") });
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  async function beginTest() {
    const payload = await placePost("/api/places/test/start", {
      camera: selectedValue("placeCamera", "palm"),
      save_images: saveImagesInput.checked,
    });
    if (payload) {
      placeReport = payload;
      renderPlaces();
    }
  }
  async function markPlace(event) {
    const payload = await placePost("/api/places/test/event", { event });
    if (payload) {
      placeReport = payload;
      renderPlaces();
    }
  }
  async function finishTest() {
    const payload = await placePost("/api/places/test/stop");
    if (payload) {
      placeReport = payload;
      renderPlaces();
    }
  }
  document.querySelectorAll("[data-mark]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.mark;
      if (kind === "start") void beginTest();
      else if (kind === "stop") void finishTest();
      else void markPlace(kind);
    });
  });
  clearTestsButton.addEventListener("click", () => {
    void (async () => {
      const payload = await placePost("/api/places/test/clear");
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  downloadPlacesButton.addEventListener("click", () => {
    void downloadPlaces();
  });
  document.getElementById("saveA").addEventListener("click", () => {
    void (async () => {
      const payload = await placePost("/api/places/reference", {
        place: "A",
        camera: "palm",
        save_images: true,
      });
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  document.getElementById("clearA").addEventListener("click", () => {
    if (!window.confirm("Remove every saved frame of place A? B and C stay.")) return;
    void (async () => {
      const payload = await placePost("/api/places/clear", { place: "A" });
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  document.getElementById("saveUnknown").addEventListener("change", (event) => {
    void placePost("/api/places/diagnostic/shots", { enabled: event.target.checked });
  });
  document.getElementById("diagnosticStart").addEventListener("click", () => {
    void (async () => {
      const payload = await placePost("/api/places/diagnostic/start", {
        camera: "palm",
        save_unknown: document.getElementById("saveUnknown").checked,
      });
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  document.getElementById("diagnosticStop").addEventListener("click", () => {
    void finishTest();
  });
  document.getElementById("applyIntervals").addEventListener("click", () => {
    void (async () => {
      const rows = [
        ["same_spot", "sameStart", "sameEnd"],
        ["arm_only", "armStart", "armEnd"],
        ["returned", "returnStart", "returnEnd"],
      ];
      const intervals = [];
      for (const [label, startId, endId] of rows) {
        const startValue = document.getElementById(startId).value;
        const endValue = document.getElementById(endId).value;
        if (!startValue && !endValue) continue;
        const start = new Date(startValue).getTime();
        const end = new Date(endValue).getTime();
        if (!startValue || !endValue || Number.isNaN(start) || Number.isNaN(end)) {
          showError("Each note needs both a start and an end.");
          return;
        }
        intervals.push({ label, start_unix_ms: start, end_unix_ms: end });
      }
      const payload = await placePost("/api/places/intervals", { intervals });
      if (payload) {
        placeReport = payload;
        renderPlaces();
      }
    })();
  });
  bindGuidedDesktop();
}

let guideReport = null;

function percentCell(value) {
  return value === null || value === undefined ? "—" : `${value}%`;
}

function statMedian(stats) {
  if (!stats || stats.median === null || stats.median === undefined) return "—";
  return String(stats.median);
}

function renderGuidedDesktop() {
  const state = document.getElementById("guideDesktopState");
  const summaryHost = document.getElementById("guideDesktopSummary");
  const correctButton = document.getElementById("correctArrival");
  if (!state || !summaryHost) return;
  const report = guideReport;
  if (!report || !report.session_id) {
    state.textContent = "No guided experiment yet. Start it on the phone.";
    summaryHost.replaceChildren();
    if (correctButton) correctButton.disabled = true;
    const trialHost = document.getElementById("trialVerify");
    if (trialHost) trialHost.hidden = true;
    return;
  }
  const viewLabel = report.mode === "returns"
    ? "Three return trials."
    : report.mode === "multi"
      ? "Several arm positions."
      : "One resting view.";
  if (report.active) {
    state.textContent = `${viewLabel} ${report.label || "Running"}. ${report.countdown} seconds left in this phase. Fresh references saved: ${report.reference_count}. This test name: ${report.predicted || "—"}.`;
  } else if (report.status === "complete") {
    state.textContent = `Session ${report.session_id} is complete. The CSV is already on this laptop.`;
  } else if (report.status === "cancelled") {
    state.textContent = `Session ${report.session_id} was stopped. Partial results are saved.`;
  } else {
    state.textContent = `Session ${report.session_id}.`;
  }
  summaryHost.replaceChildren();
  const summary = report.summary;
  if (!summary) return;
  const notes = document.createElement("div");
  (summary.comparison || []).forEach((line) => {
    const paragraph = document.createElement("p");
    paragraph.className = "detail";
    paragraph.textContent = line;
    notes.appendChild(paragraph);
  });
  if (summary.reference_count === 0) {
    const warning = document.createElement("p");
    warning.className = "detail";
    warning.textContent = "No fresh reference frames were saved. Later phases cannot name A.";
    notes.appendChild(warning);
  } else {
    const saved = document.createElement("p");
    saved.className = "detail";
    const viewDetail = Array.isArray(summary.views) && summary.views.length
      ? ` By view: ${summary.views.map((item) => `${item.title} ${item.references}`).join(", ")}.`
      : "";
    saved.textContent = `${summary.reference_count} fresh Location A frames were saved for this session. The older saved places were not changed.${viewDetail}`;
    notes.appendChild(saved);
  }
  summaryHost.appendChild(notes);
  const table = document.createElement("table");
  table.className = "timeline";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["Phase", "Frames", "Raw A", "Stable A", "UNKNOWN", "Zero features", "Median inliers", "Bursts"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.appendChild(cell);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  (summary.phases || []).forEach((phase) => {
    const row = document.createElement("tr");
    const title = phase.role === "transition" ? `${phase.title} (transition)` : phase.role === "reference" ? `${phase.title} (references)` : phase.title;
    [title, phase.frames, percentCell(phase.raw_a_percent), percentCell(phase.stable_a_percent), percentCell(phase.unknown_percent), phase.zero_feature_frames, statMedian(phase.features && phase.inliers ? phase.inliers : phase.inliers), phase.bursts].forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 6) cell.textContent = statMedian(phase.inliers);
      else cell.textContent = String(value);
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  if (summary.corrected_return) {
    const phase = summary.corrected_return;
    const row = document.createElement("tr");
    [phase.title, phase.frames, percentCell(phase.raw_a_percent), percentCell(phase.stable_a_percent), percentCell(phase.unknown_percent), phase.zero_feature_frames, statMedian(phase.inliers), phase.bursts].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.appendChild(cell);
    });
    body.appendChild(row);
  }
  table.appendChild(body);
  summaryHost.appendChild(table);
  renderReturnTrials(summaryHost, summary);
  const singleLate = document.getElementById("singleLateRow");
  if (singleLate) singleLate.hidden = report.mode === "returns";
  ensureTrialVerify(report);
  if (correctButton) correctButton.disabled = Boolean(report.active) || report.mode === "returns";
}

function latencyText(milliseconds, verified) {
  if (!verified) return "not verified";
  if (milliseconds === null || milliseconds === undefined) return "before the confirmed arrival";
  return `${(milliseconds / 1000).toFixed(1)} s`;
}

function renderReturnTrials(host, summary) {
  if (!summary || !summary.trials) return;
  const table = document.createElement("table");
  table.className = "timeline";
  const head = document.createElement("tr");
  ["Trial", "At desk raw", "At desk stable", "Return raw", "Return stable", "Raw latency", "Stable latency", "A while leaving"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    head.appendChild(cell);
  });
  const headWrap = document.createElement("thead");
  headWrap.appendChild(head);
  table.appendChild(headWrap);
  const body = document.createElement("tbody");
  summary.trials.forEach((trial) => {
    const held = trial.return_hold;
    const row = document.createElement("tr");
    [
      String(trial.trial),
      percentCell(trial.at_desk.raw_a_percent),
      percentCell(trial.at_desk.stable_a_percent),
      percentCell(held.raw_a_percent),
      percentCell(held.stable_a_percent),
      latencyText(held.raw_latency_ms, held.arrival_verified),
      latencyText(held.stable_latency_ms, held.arrival_verified),
      `${trial.while_leaving.raw_a} of ${trial.while_leaving.frames}`,
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  table.appendChild(body);
  host.appendChild(table);
  if (summary.unfamiliar) {
    const note = document.createElement("p");
    note.className = "detail";
    note.textContent = `Unfamiliar hold: ${summary.unfamiliar.incorrect_raw} of ${summary.unfamiliar.frames} frames named A. Those are the incorrect matches. ${summary.unfamiliar.note}`;
    host.appendChild(note);
  }
  const banks = summary.bank_comparison;
  if (!banks || !banks.phases) return;
  const bankNote = document.createElement("p");
  bankNote.className = "detail";
  bankNote.textContent = banks.note;
  host.appendChild(bankNote);
  const bankTable = document.createElement("table");
  bankTable.className = "timeline";
  const bankHead = document.createElement("tr");
  ["Held-out phase", "Frames", "4 resting views raw", "4 mixed views raw"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    bankHead.appendChild(cell);
  });
  const bankHeadWrap = document.createElement("thead");
  bankHeadWrap.appendChild(bankHead);
  bankTable.appendChild(bankHeadWrap);
  const bankBody = document.createElement("tbody");
  banks.phases.forEach((phase) => {
    const row = document.createElement("tr");
    [phase.id, phase.frames, percentCell(phase.single_raw_percent), percentCell(phase.multi_raw_percent)].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.appendChild(cell);
    });
    bankBody.appendChild(row);
  });
  bankTable.appendChild(bankBody);
  host.appendChild(bankTable);
}

function ensureTrialVerify(report) {
  const host = document.getElementById("trialVerify");
  if (!host) return;
  if (!report || report.mode !== "returns" || !report.session_id || report.active) {
    host.hidden = true;
    if (!report || report.mode !== "returns") host.dataset.session = "";
    return;
  }
  host.hidden = false;
  if (host.dataset.session === report.session_id) return;
  host.dataset.session = report.session_id;
  host.replaceChildren();
  const note = document.createElement("p");
  note.className = "note";
  note.textContent = "Confirm a trial only if you know you were at the desk. Zero means the hold started after you arrived. Leave a trial unset if you are not sure.";
  host.appendChild(note);
  [1, 2, 3].forEach((trial) => {
    const row = document.createElement("div");
    row.className = "interval-row";
    const label = document.createElement("span");
    label.textContent = `Trial ${trial} seconds late`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "30";
    input.value = "0";
    input.setAttribute("aria-label", `Trial ${trial} seconds late`);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = `Confirm trial ${trial}`;
    button.addEventListener("click", () => {
      const late = Number(input.value);
      if (!Number.isInteger(late) || late < 0 || late > 30) {
        showError("Enter a whole number of seconds from 0 to 30.");
        return;
      }
      void (async () => {
        const payload = await placePost("/api/places/experiment/arrival", {
          session_id: report.session_id,
          late_seconds: late,
          trial,
        });
        if (payload) {
          guideReport = payload;
          renderGuidedDesktop();
        }
      })();
    });
    row.append(label, input, button);
    host.appendChild(row);
  });
}

async function pollGuidedDesktop() {
  if (!token) return;
  try {
    const response = await fetch("/api/places/experiment", {
      cache: "no-store",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) return;
    guideReport = await response.json();
  } catch (_error) {
    guideReport = null;
  } finally {
    renderGuidedDesktop();
  }
}

function bindGuidedDesktop() {
  void pollGuidedDesktop();
  setInterval(pollGuidedDesktop, 500);
  const correctButton = document.getElementById("correctArrival");
  const downloadButton = document.getElementById("downloadExperiment");
  if (correctButton) {
    correctButton.addEventListener("click", () => {
      void (async () => {
        if (!guideReport || !guideReport.session_id) return;
        const late = Number(document.getElementById("lateSeconds").value);
        if (!Number.isInteger(late) || late < 0 || late > 30) {
          showError("Enter a whole number of seconds from 0 to 30.");
          return;
        }
        const payload = await placePost("/api/places/experiment/arrival", {
          session_id: guideReport.session_id,
          late_seconds: late,
        });
        if (payload) {
          guideReport = payload;
          renderGuidedDesktop();
        }
      })();
    });
  }
  if (downloadButton) {
    downloadButton.addEventListener("click", () => {
      void (async () => {
        const session = guideReport && guideReport.session_id ? `?session_id=${guideReport.session_id}` : "";
        const response = await fetch(`/api/places/experiment.csv${session}`, {
          cache: "no-store",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) {
          showError("Could not download the experiment CSV.");
          return;
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = guideReport && guideReport.session_id ? `retrace-experiment-${guideReport.session_id}.csv` : "retrace-experiment.csv";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      })();
    });
  }
}
