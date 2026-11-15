const token = new URLSearchParams(location.search).get("token") || "";
const note = document.getElementById("note");

function headers(json) {
  const result = { "X-Retrace": "1", "X-Retrace-Token": token };
  if (json) result["Content-Type"] = "application/json";
  return result;
}

async function call(path, method) {
  const response = await fetch(path, { method, headers: headers(false) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "Request failed");
  return body;
}

async function callJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(payload || {}),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "Request failed");
  return body;
}

function line(id, text) {
  document.getElementById(id).textContent = text;
}

async function refresh() {
  const response = await fetch("/api/state?token=" + encodeURIComponent(token), {
    headers: { "X-Retrace-Token": token },
  });
  if (!response.ok) return;
  const state = await response.json();
  const link = state.link || {};
  const imu = link.imu || {};
  const pdr = state.pdr || {};
  const test = state.pdr_test || {};
  line("ring", "Ring connected: " + (link.connected ? "yes" : "no"));
  line("camera", "Camera: " + (test.camera_paused ? "paused" : "on"));
  line("rate", "IMU rate: " + Number(imu.rate_hz || 0).toFixed(0) + " Hz");
  const walking = pdr.walking === true;
  line("gate", "Walking gate: " + (walking ? "open" : "closed"));
  const steps = pdr.steps != null ? pdr.steps : test.steps;
  line("steps", steps == null ? "Steps: —" : "Steps: " + Number(steps).toFixed(1));
  const bits = [];
  if (test.steps != null) bits.push("Steps " + Number(test.steps).toFixed(1));
  if (test.path_m != null) bits.push("Path " + Number(test.path_m).toFixed(2) + " m");
  if (test.closure_error_m != null) bits.push("Closure error " + Number(test.closure_error_m).toFixed(2) + " m");
  line("result", bits.join(" · "));
  line("drops", test.drop_report || "");
  const active = !!test.active;
  for (const id of ["calib", "swing", "carry"]) {
    document.getElementById(id).disabled = active;
  }
  document.getElementById("end").disabled = !active;
  document.getElementById("resume").disabled = active || !test.camera_paused;
  if (test.phase === "settling") {
    note.textContent = "Pictures are paused. Waiting until the motion stream stays above 80 Hz.";
  } else if (test.error) note.textContent = test.error;
  else if (test.phase === "walk" && test.kind === "calib") {
    note.textContent = "Walk a known distance, type the metres, and press End.";
  } else if (test.phase === "walk" && test.marks >= 1) {
    note.textContent = "Walk back to the same point and press End.";
  } else if (test.phase === "walk") {
    note.textContent = "Stand on the start point and press End, then walk the loop.";
  }
  const items = state.items || [];
  const latest = items.reduce((best, item) => {
    if (!best || item.created_unix_ms > best.created_unix_ms) return item;
    return best;
  }, null);
  document.getElementById("item").textContent = latest ? latest.name : "No item yet";
  document.getElementById("find").disabled = !latest;
  document.getElementById("find").dataset.id = latest ? String(latest.id) : "";
  const find = state.find;
  const heat = find ? Math.max(0, Math.min(1, Number(find.heat || 0))) : 0;
  document.getElementById("fill").style.width = Math.round(heat * 100) + "%";
  document.getElementById("status").textContent = find
    ? (find.status || "Finding") + (find.guidance ? " — " + find.guidance : "")
    : "Idle";
}

async function start(kind) {
  try {
    await callJson("/api/pdr/start", { kind });
    note.textContent = "";
  } catch (error) {
    note.textContent = error.message;
  }
}

document.getElementById("calib").addEventListener("click", () => start("calib"));
document.getElementById("swing").addEventListener("click", () => start("swing"));
document.getElementById("carry").addEventListener("click", () => start("carry"));
document.getElementById("end").addEventListener("click", async () => {
  const raw = document.getElementById("meters").value.trim();
  const payload = {};
  if (raw !== "") {
    const meters = Number(raw);
    if (!Number.isFinite(meters) || meters <= 0) {
      note.textContent = "Type the distance in metres, then press End.";
      return;
    }
    payload.meters = meters;
  }
  try {
    const body = await callJson("/api/pdr/end", payload);
    if (body.error) note.textContent = body.error;
  } catch (error) {
    note.textContent = error.message;
  }
});

document.getElementById("resume").addEventListener("click", async () => {
  try {
    await call("/api/pdr/resume-camera", "POST");
    note.textContent = "Camera resumed.";
  } catch (error) {
    note.textContent = error.message;
  }
});

document.getElementById("mark").addEventListener("click", async () => {
  try {
    await call("/api/putdown/mark", "POST");
    note.textContent = "Marked.";
  } catch (error) {
    note.textContent = error.message;
  }
});
document.getElementById("find").addEventListener("click", async () => {
  const id = document.getElementById("find").dataset.id;
  if (!id) return;
  try {
    await call("/api/items/" + id + "/find", "POST");
    note.textContent = "Finding the latest item.";
  } catch (error) {
    note.textContent = error.message;
  }
});
document.getElementById("stop").addEventListener("click", async () => {
  try {
    await call("/api/find/stop", "POST");
    note.textContent = "Find stopped.";
  } catch (error) {
    note.textContent = error.message;
  }
});
refresh();
setInterval(refresh, 400);
