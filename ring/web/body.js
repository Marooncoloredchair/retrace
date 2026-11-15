"use strict";

const status = document.getElementById("status");
let messageId = 1;
let heading = null;
let accel = null;
let sent = 0;
const sessionId = "phone-page";

function show(text) {
  status.textContent = text;
}

function sample() {
  const payload = [];
  const time = Math.round(Date.now() * 1e6);
  if (heading != null) {
    payload.push({
      name: "compass",
      time,
      values: { magneticBearing: heading },
    });
  }
  if (accel) {
    payload.push({
      name: "accelerometer",
      time,
      values: { x: accel.x, y: accel.y, z: accel.z },
    });
  }
  if (!payload.length) return;
  fetch("/api/sensor-logger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messageId: messageId++,
      sessionId,
      deviceId: "phone-page",
      payload,
    }),
  }).then((response) => {
    if (!response.ok) throw new Error(String(response.status));
    sent += 1;
    show(`Sending. Heading ${heading == null ? "—" : heading.toFixed(0) + "°"}. ${sent} packets accepted.`);
  }).catch(() => {
    show("The laptop refused the packet. Stay on the same Wi-Fi as the laptop.");
  });
}

window.addEventListener("deviceorientation", (event) => {
  if (typeof event.webkitCompassHeading === "number" && Number.isFinite(event.webkitCompassHeading)) {
    heading = event.webkitCompassHeading;
  }
});

window.addEventListener("devicemotion", (event) => {
  const reading = event.acceleration;
  if (!reading || reading.x == null) return;
  accel = { x: reading.x, y: reading.y, z: reading.z };
});

const saveStatus = document.getElementById("save-status");
const guideLine = document.getElementById("guide-line");
const items = document.getElementById("items");
const guideCanvas = document.getElementById("guide");

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "The laptop refused that.");
  return payload;
}

document.getElementById("save-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = document.getElementById("save-name").value.trim();
  if (!name) {
    saveStatus.textContent = "Type a name first.";
    return;
  }
  try {
    await postJson("/api/body/save", { name });
    document.getElementById("save-name").value = "";
    saveStatus.textContent = `${name} saved with the phone position.`;
    refresh();
  } catch (error) {
    saveStatus.textContent = error.message;
  }
});

document.addEventListener("keydown", (event) => {
  const tag = (event.target && event.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (event.key === "p" || event.key === "P") {
    event.preventDefault();
    document.getElementById("save-form").requestSubmit();
  }
});

function drawGuide(live) {
  const ctx = guideCanvas.getContext("2d");
  const w = guideCanvas.width;
  const h = guideCanvas.height;
  const cx = w / 2;
  const cy = h / 2;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#2a3139";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(cx, cy, 100, 0, Math.PI * 2);
  ctx.stroke();
  const find = live && live.find;
  const guide = live && live.body && live.body.guide;
  if (!find || !guide || guide.distance_m == null) {
    ctx.fillStyle = "#66707a";
    ctx.font = "14px system-ui";
    ctx.textAlign = "center";
    ctx.fillText("no find", cx, cy);
    guideLine.textContent = "Save an item, then press Find.";
    return;
  }
  const error = Number(guide.error_deg) || 0;
  const radians = error * Math.PI / 180;
  ctx.strokeStyle = "#5aa9f0";
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.lineTo(cx + Math.sin(radians) * 88, cy - Math.cos(radians) * 88);
  ctx.stroke();
  ctx.fillStyle = "#e7edf2";
  ctx.font = "20px system-ui";
  ctx.textAlign = "center";
  ctx.fillText(`${Number(guide.distance_m).toFixed(1)} m`, cx, cy + 8);
  const turn = error > 1 ? "right" : error < -1 ? "left" : "straight";
  guideLine.textContent =
    `${Number(guide.distance_m).toFixed(1)} m, bearing ${Number(guide.bearing).toFixed(0)}°, ` +
    `error ${Math.abs(error).toFixed(0)}° ${turn}.`;
}

async function refresh() {
  try {
    const response = await fetch("/api/body/live");
    if (!response.ok) return;
    const live = await response.json();
    drawGuide(live);
    items.replaceChildren();
    for (const item of live.items || []) {
      const row = document.createElement("li");
      const current = live.body && live.body.frame_id;
      const same = item.x != null && item.body_frame && item.body_frame === current;
      const where = item.x == null
        ? "no position"
        : same
          ? `${Number(item.x).toFixed(1)} m east, ${Number(item.y).toFixed(1)} m north`
          : "from an earlier session";
      row.textContent = `${item.name} (${where}) `;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Find";
      button.disabled = !same;
      button.addEventListener("click", async () => {
        try {
          await postJson("/api/body/find", { item_id: item.id });
          saveStatus.textContent = `Finding ${item.name}.`;
          refresh();
        } catch (error) {
          saveStatus.textContent = error.message;
        }
      });
      row.append(button);
      items.append(row);
    }
  } catch (error) {
    guideLine.textContent = "The phone cannot see the laptop.";
  }
}

refresh();
setInterval(refresh, 500);

document.getElementById("enable").addEventListener("click", async () => {
  const orientation = window.DeviceOrientationEvent;
  const motion = window.DeviceMotionEvent;
  try {
    if (orientation && typeof orientation.requestPermission === "function") {
      await orientation.requestPermission();
    }
    if (motion && typeof motion.requestPermission === "function") {
      await motion.requestPermission();
    }
  } catch (error) {
    show("The phone did not allow motion. Use Sensor Logger's HTTP push.");
    return;
  }
  show("Waiting for a compass reading.");
  setInterval(sample, 200);
  setTimeout(() => {
    if (heading == null && !accel) {
      show("No compass arrived. iPhone Safari will not share it on this plain HTTP page. Use Sensor Logger's HTTP push.");
    }
  }, 4000);
});
