"use strict";

// Same Save, Find, and live poll as the /body phone page. This file only draws them.

const SAVE = "/api/body/save";
const FIND = "/api/body/find";
const STOP = "/api/body/find/stop";
const LIVE = "/api/body/live";
const HEALTH = "/api/health";

let itemsKey = "";
let saveBusy = false;
let polling = false;

function $(id) {
  return document.getElementById(id);
}

function setPill(el, text, kind) {
  el.textContent = text;
  el.className = "pill" + (kind ? " " + kind : "");
}

function gatePhrase(body) {
  if (!body || typeof body.advancing !== "boolean") return "";
  if (body.advancing) return "advancing";
  const reason = typeof body.hold_reason === "string" ? body.hold_reason.trim() : "";
  return reason ? "held " + reason : "held";
}

function tracking(body) {
  return !!(body && body.packet_age_s != null && Number(body.packet_age_s) <= 3);
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === "string" && detail ? detail : "The laptop refused that.");
  }
  return payload;
}

function whereText(item, frame) {
  if (item.x == null) return "No position";
  const same = item.body_frame && item.body_frame === frame;
  if (!same) return "From an earlier session";
  return Number(item.x).toFixed(1) + " m east, " + Number(item.y).toFixed(1) + " m north";
}

function renderItems(live) {
  const items = (live && live.items) || [];
  const frame = live && live.body ? live.body.frame_id : "";
  const key = String(frame || "") + "|" + items.map((item) =>
    [item.id, item.name, item.x, item.y, item.body_frame].join(":")
  ).join("|");
  $("empty").hidden = items.length > 0;
  if (key === itemsKey) return;
  itemsKey = key;
  const list = $("items");
  list.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.name || "Item";
    const where = document.createElement("span");
    where.className = "item-where";
    where.textContent = whereText(item, frame);
    button.append(name, where);
    button.addEventListener("click", () => findItem(item));
    row.append(button);
    list.append(row);
  });
}

function turnLine(guide) {
  if (!guide || typeof guide.error_deg !== "number" || Number.isNaN(guide.error_deg)) {
    return "Waiting for a heading.";
  }
  if (guide.arrived === true || guide.facing === true || Math.abs(guide.error_deg) < 1) {
    return "Straight ahead.";
  }
  return guide.error_deg > 0 ? "To your right." : "To your left.";
}

function paintArrow(guide) {
  const error = guide && typeof guide.error_deg === "number" && !Number.isNaN(guide.error_deg)
    ? guide.error_deg
    : null;
  const facing = !!(guide && (guide.arrived === true || guide.facing === true || (error != null && Math.abs(error) < 1)));
  const mark = $("arrow-mark");
  mark.setAttribute("transform", "rotate(" + (error == null ? 0 : error) + " 50 50)");
  mark.setAttribute("fill", error == null ? "#8A94A6" : (facing ? "#2FBF71" : "#FFB020"));
  $("turn").classList.toggle("facing", facing && error != null);
}

function drawPin(ctx, x, y, dpr) {
  ctx.fillStyle = "#FFB020";
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x - 6.5 * dpr, y - 9 * dpr);
  ctx.lineTo(x + 6.5 * dpr, y - 9 * dpr);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.arc(x, y - 12 * dpr, 5.5 * dpr, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#0F1420";
  ctx.beginPath();
  ctx.arc(x, y - 12 * dpr, 2.2 * dpr, 0, Math.PI * 2);
  ctx.fill();
}

function drawMap(body) {
  const canvas = $("map");
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  if (cssW < 2 || cssH < 2) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.round(cssW * dpr);
  const height = Math.round(cssH * dpr);
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  const ctx = canvas.getContext("2d");
  const path = (body && body.path) || [];
  const guide = body && body.guide;
  const points = [];
  path.forEach((point) => {
    if (point && point[0] != null && point[1] != null) points.push([point[0], point[1]]);
  });
  if (body && body.x != null && body.y != null) points.push([body.x, body.y]);
  if (guide && guide.target_x != null && guide.target_y != null) points.push([guide.target_x, guide.target_y]);
  ctx.clearRect(0, 0, width, height);
  if (!points.length) return;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  points.forEach((point) => {
    minX = Math.min(minX, point[0]);
    maxX = Math.max(maxX, point[0]);
    minY = Math.min(minY, point[1]);
    maxY = Math.max(maxY, point[1]);
  });
  const pad = 0.8;
  minX -= pad;
  maxX += pad;
  minY -= pad;
  maxY += pad;
  const margin = 18 * dpr;
  const scale = Math.min(
    (width - margin * 2) / Math.max(maxX - minX, 0.5),
    (height - margin * 2) / Math.max(maxY - minY, 0.5),
  );
  const toX = (x) => (x - minX) * scale + margin;
  const toY = (y) => height - ((y - minY) * scale + margin);
  ctx.lineWidth = 2.5 * dpr;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.strokeStyle = "#8A94A6";
  ctx.beginPath();
  path.forEach((point, index) => {
    if (!point || point[0] == null) return;
    const x = toX(point[0]);
    const y = toY(point[1]);
    const broken = index > 0 && point[2] === 1;
    if (index === 0 || broken) {
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x, y);
      if (broken) {
        ctx.fillStyle = "#FF5A4D";
        ctx.beginPath();
        ctx.arc(x, y, 3.5 * dpr, 0, Math.PI * 2);
        ctx.fill();
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.strokeStyle = "#8A94A6";
      }
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  const here = path.length ? path[path.length - 1] : [body.x, body.y];
  if (guide && here && here[0] != null && guide.target_x != null) {
    ctx.save();
    ctx.setLineDash([5 * dpr, 4 * dpr]);
    ctx.strokeStyle = "#FFB020";
    ctx.beginPath();
    ctx.moveTo(toX(here[0]), toY(here[1]));
    ctx.lineTo(toX(guide.target_x), toY(guide.target_y));
    ctx.stroke();
    ctx.restore();
  }
  if (guide && guide.target_x != null && guide.target_y != null) {
    drawPin(ctx, toX(guide.target_x), toY(guide.target_y), dpr);
  }
  if (here && here[0] != null && here[1] != null) {
    const x = toX(here[0]);
    const y = toY(here[1]);
    ctx.fillStyle = "#0F1420";
    ctx.beginPath();
    ctx.arc(x, y, 7 * dpr, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#4C8DF6";
    ctx.beginPath();
    ctx.arc(x, y, 5 * dpr, 0, Math.PI * 2);
    ctx.fill();
  }
}

function paint(live, ring) {
  if (ring === true) setPill($("ring-pill"), "Ring: connected", "ok");
  else if (ring === false) setPill($("ring-pill"), "Ring: disconnected", "bad");
  else setPill($("ring-pill"), "Ring: —", "");
  if (!live) {
    $("offline").hidden = false;
    return;
  }
  $("offline").hidden = true;
  const body = live.body || null;
  setPill($("phone-pill"), tracking(body) ? "Phone: tracking" : "Phone: not tracking", tracking(body) ? "ok" : "bad");
  const gate = $("gate-pill");
  const phrase = gatePhrase(body);
  gate.hidden = !phrase;
  if (phrase) setPill(gate, phrase, body.advancing ? "ok" : "hold");
  const finding = !!live.find;
  $("home").hidden = finding;
  $("find").hidden = !finding;
  if (!finding) {
    renderItems(live);
    return;
  }
  itemsKey = "";
  const guide = body && body.guide;
  const find = live.find;
  $("find-name").textContent = (find && find.name) || (guide && guide.name) || "";
  $("turn").textContent = turnLine(guide);
  paintArrow(guide);
  const fromGuide = guide && guide.distance_m != null ? Number(guide.distance_m) : null;
  const fromFind = find && find.distance_m != null ? Number(find.distance_m) : null;
  const value = fromGuide != null && !Number.isNaN(fromGuide) ? fromGuide : fromFind;
  if (value == null || Number.isNaN(value)) {
    $("metres").textContent = "—";
    $("unit").hidden = true;
  } else {
    $("metres").textContent = value.toFixed(1);
    $("unit").hidden = false;
  }
  $("distance").classList.toggle("here", !!(guide && guide.arrived === true));
  drawMap(body || {});
}

async function refresh() {
  if (polling) return;
  polling = true;
  let live = null;
  let ring = null;
  try {
    const response = await fetch(LIVE);
    if (response.ok) live = await response.json();
  } catch (error) {
    live = null;
  }
  try {
    const response = await fetch(HEALTH);
    if (response.ok) {
      const health = await response.json();
      if (health && typeof health.ring_connected === "boolean") ring = health.ring_connected;
    }
  } catch (error) {
    ring = null;
  }
  try {
    paint(live, ring);
  } finally {
    polling = false;
  }
}

async function findItem(item) {
  $("note").textContent = "";
  try {
    await postJson(FIND, { item_id: item.id });
    $("note").textContent = "Finding " + (item.name || "item") + ".";
    refresh();
  } catch (error) {
    $("note").textContent = error.message;
  }
}

$("save-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (saveBusy) return;
  const input = $("save-name");
  const name = input.value.trim();
  if (!name) {
    $("note").textContent = "Type a name first.";
    return;
  }
  saveBusy = true;
  $("save-button").disabled = true;
  try {
    await postJson(SAVE, { name });
    input.value = "";
    $("note").textContent = name + " saved.";
    itemsKey = "";
    refresh();
  } catch (error) {
    $("note").textContent = error.message;
  } finally {
    saveBusy = false;
    $("save-button").disabled = false;
  }
});

$("stop").addEventListener("click", async () => {
  $("stop").disabled = true;
  try {
    await postJson(STOP, {});
    $("note").textContent = "";
    refresh();
  } catch (error) {
    $("note").textContent = error.message;
  } finally {
    $("stop").disabled = false;
  }
});

refresh();
setInterval(refresh, 500);
