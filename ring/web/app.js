"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let socket = null;
let liveUrl = "";
let selectedItem = null;
let timelineKey = "";
let detail = null;
let detailKey = "";
let itemsKey = "";
let recentKey = "";
let saveNote = null;
let saveBusy = false;

function clock(unixMs) {
  if (!unixMs) return "";
  return new Date(unixMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function keyframeUrl(id) {
  return `/api/keyframes/${id}.jpg`;
}

function setPill(element, text, kind) {
  element.textContent = text;
  element.className = "pill " + kind;
}

async function post(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-ReTrace": "1" },
      body: JSON.stringify(body || {}),
    });
  } catch (error) {
    throw new Error("API error: ReTrace did not answer. Check that it is still running in its terminal window.");
  }
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (!response.ok) {
    const text = typeof data.detail === "string" && data.detail
      ? data.detail
      : `API error: ReTrace answered ${response.status} without a reason. Look at its terminal window.`;
    const failure = new Error(text);
    failure.code = data.code || "";
    throw failure;
  }
  return data;
}

function banner(element, kind, title, text) {
  element.hidden = !text;
  if (!text) return;
  element.className = "banner " + kind;
  const head = document.createElement("strong");
  head.textContent = title;
  element.replaceChildren(head, text);
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket.binaryType = "blob";
  socket.onopen = () => setPill($("pill-service"), "Dashboard: live", "good");
  socket.onmessage = (event) => {
    if (typeof event.data === "string") {
      state = JSON.parse(event.data);
      render();
      return;
    }
    const url = URL.createObjectURL(event.data);
    $("live").src = url;
    if (liveUrl) URL.revokeObjectURL(liveUrl);
    liveUrl = url;
  };
  socket.onclose = () => {
    setPill($("pill-service"), "Dashboard: ReTrace is not running", "bad");
    $("live-cover").hidden = false;
    $("live-cover").textContent = "ReTrace is not running. Start it again, then this page reconnects.";
    setTimeout(connect, 1000);
  };
}

function renderMode() {
  const mode = state.mode || {};
  const memory = $("mode-memory");
  memory.textContent = mode.memory || "CONTINUOUS MEMORY ACTIVE";
  memory.className = "mode on";
  const save = $("mode-save");
  save.textContent = mode.save || "NO ITEM YET";
  save.className = "mode " + (mode.save === "ITEM SAVED" ? "on" : "quiet");
  const anchor = $("mode-anchor");
  const anchorText = mode.anchor || "ANCHOR -";
  anchor.textContent = anchorText;
  let anchorKind = "quiet";
  if (anchorText.includes("READY")) anchorKind = "on";
  else if (anchorText.includes("LIMITED")) anchorKind = "warn";
  else if (anchorText.includes("MISSING")) anchorKind = "bad";
  anchor.className = "mode " + anchorKind;
  const search = $("mode-search");
  search.textContent = mode.search || "IDLE";
  let searchKind = "quiet";
  if (mode.search === "ARRIVED") searchKind = "on";
  else if (mode.search === "PROGRESS CONFIRMED" || mode.search === "LANDMARK RECOGNIZED") searchKind = "search";
  else if (mode.search === "TRACKING LOST" || mode.search === "TURN PALM OUTWARD") searchKind = "warn";
  else if (mode.search === "SEARCHING") searchKind = "search";
  search.className = "mode " + searchKind;
}

function renderHeader() {
  const link = state.link;
  const imu = link.imu || {};
  if (link.connected && link.transport === "wifi") {
    setPill(
      $("pill-link"),
      `Ring: Wi-Fi ${link.camera_fps} fps, IMU ${imu.rate_hz || 0} Hz, dropped ${imu.dropped || 0}, ${link.latency_ms || 0} ms, ${link.rssi_dbm ?? "?"} dBm`,
      "good",
    );
  } else if (link.connected) {
    setPill($("pill-link"), `Ring: connected (${link.port}, ${link.camera_fps} frames/s)`, "good");
  } else {
    setPill($("pill-link"), "Ring: disconnected", "bad");
  }
  if (imu.ok) setPill($("pill-imu"), `IMU: OK (${imu.rate_hz} samples/s)`, "good");
  else setPill($("pill-imu"), link.connected ? "IMU: no samples" : "IMU: -", "bad");
  const motor = state.motor;
  if (!motor.writes_allowed) setPill($("pill-motor"), "Vibration: off for this run", "warn");
  else if (!motor.enabled) setPill($("pill-motor"), "Vibration: switched off", "warn");
  else setPill($("pill-motor"), `Vibration: ready (${motor.commands_sent} sent)`, "good");
  const mapper = state.mapper;
  setPill($("pill-mapper"), `Mapper: ${mapper.name}${mapper.state === "off" ? "" : " " + mapper.state}`, "quiet");
  $("pill-mapper").title = mapper.detail || "";

  $("live-cover").hidden = link.connected;
  if (!link.connected) $("live-cover").textContent = link.error || "Ring not connected";

  const last = state.frames.last || {};
  if (!link.connected) {
    $("frame-line").textContent = "No camera. Saved items are safe. Continuous memory resumes when the ring reconnects.";
  } else if (last.features === undefined) {
    $("frame-line").textContent = `Live view: ${last.text || "waiting"}.`;
  } else {
    const occ = last.occlusion === undefined ? "" : `, occlusion ${Math.round((last.occlusion || 0) * 100)}%`;
    $("frame-line").textContent =
      `Live view: ${last.text}. Detail points ${last.visible_features ?? last.features} visible / ${last.features} total, ` +
      `sharpness ${last.sharpness}, coverage ${Math.round((last.coverage || 0) * 100)}%${occ}. ` +
      `Observations ${state.frames.observed}, landmarks ${state.frames.landmarks}, looked at ${state.frames.processed}.`;
  }
}

function renderTracking() {
  const find = state.find;
  const status = $("track-status");
  const figMatched = $("fig-matched");
  const figNext = $("fig-next");
  const heatWrap = $("heat-wrap");
  const placeCard = $("place-card");
  $("btn-stop-find").hidden = !find;
  if (!find) {
    status.textContent = "IDLE";
    status.className = "status idle";
    $("track-phase").textContent = "";
    $("track-text").textContent = state.capture
      ? `Saving ${state.capture.name}: collecting context (${state.capture.seconds_left} s left).`
      : "Press Find on a saved item to start.";
    $("track-detail").textContent = "";
    figMatched.hidden = true;
    figNext.hidden = true;
    heatWrap.hidden = true;
    placeCard.hidden = true;
    return;
  }
  status.textContent = find.status;
  status.className = "status " + String(find.status || "idle").toLowerCase();
  $("track-phase").textContent = find.phase || "";
  $("track-text").textContent = find.guidance || "";
  const isPlace = find.backend === "place" || find.backend === "pdr";
  heatWrap.hidden = !isPlace;
  placeCard.hidden = !isPlace;
  if (isPlace) {
    const heat = Number(find.heat || 0);
    $("heat-value").textContent = heat.toFixed(2);
    $("heat-fill").style.width = `${Math.round(Math.max(0, Math.min(1, heat)) * 100)}%`;
    const bits = [];
    if (find.inliers != null) bits.push(`${find.inliers} inliers`);
    if (find.match_ms) bits.push(`${find.match_ms} ms`);
    if (find.vram_mb != null) bits.push(`${find.vram_mb} MB VRAM`);
    if (find.distance_m != null) bits.unshift(`${find.distance_m} m`);
    if (find.pulse_interval_s) bits.push(`pulse every ${find.pulse_interval_s}s`);
    if (find.silent) bits.push("silent — turn palm outward");
    $("heat-meta").textContent = bits.join(" · ");
    $("place-name").textContent = find.name || "";
    $("place-desc").textContent = find.place ? `Place: ${find.place}` : "";
    const lastSeen = find.last_seen_keyframe;
    $("fig-lastseen").hidden = !lastSeen;
    if (lastSeen) {
      const url = keyframeUrl(lastSeen);
      if (!$("img-lastseen").src.endsWith(url)) $("img-lastseen").src = url;
    }
  }
  const reading = find.reading;
  let detailText = find.backend === "pdr"
    ? "Dead reckoning from the ring IMU. Heat is 1 at the saved pin and 0 at 6 m. Under 1 m the last-seen photo stays up."
    : isPlace
    ? "Place-find uses DINOv2 heat; arrival uses LightGlue on ALIKED features."
    : "Breadcrumb numbers are an order in time, not a distance.";
  if (reading) {
    detailText = `This frame: ${reading.note}` +
      (reading.inliers ? ` (${reading.inliers} agreeing points)` : "") +
      `. Compared ${find.frames_compared} frames` +
      (find.frames_blocked ? `, paused for occlusion on ${find.frames_blocked}` : "") +
      `, ${find.match_ms} ms each. ` + detailText;
  }
  if (find.backend === "body") {
    detailText = "The phone in your pocket is the position. The ring buzzes faster as you get closer: about 1.8 s far away, about 0.4 s near the pin, and a rapid buzz under 1 m.";
    if (!state.link.connected) detailText += " The ring is not connected, so the buzz waits until it is.";
  } else if (!state.link.connected) {
    detailText = "The ring is disconnected, so nothing can be compared. " + detailText;
  }
  $("track-detail").textContent = detailText;

  const matched = isPlace
    ? find.best_anchor_keyframe || find.matched_keyframe
    : (find.status === "ARRIVED"
      ? (find.references[find.references.length - 1] || find.matched_keyframe)
      : find.matched_keyframe);
  figMatched.hidden = !matched;
  if (matched) {
    const url = keyframeUrl(matched);
    if (!$("img-matched").src.endsWith(url)) $("img-matched").src = url;
    $("cap-matched").textContent = find.backend === "pdr"
      ? "Last-seen photo"
      : isPlace
      ? (find.status === "ARRIVED" ? "Best matching place anchor" : "Best heat match (anchor)")
      : (find.status === "ARRIVED" ? "Saved spot" : "Recognised landmark (recorded picture)");
  }
  figNext.hidden = isPlace || !find.next_keyframe;
  if (!isPlace && find.next_keyframe) {
    const url = keyframeUrl(find.next_keyframe);
    if (!$("img-next").src.endsWith(url)) $("img-next").src = url;
    $("cap-next").textContent = `Next recorded view to look for (breadcrumb ${Math.max(0, (find.confirmed_stage || 1) - 1)}).`;
  }
}

function sameFrame(item) {
  const frame = state.body && state.body.frame_id;
  return item.x != null && item.y != null && item.body_frame && item.body_frame === frame;
}

function itemLine(item) {
  if (item.x != null && item.y != null && !sameFrame(item)) return "From an earlier session.";
  if (item.x != null && item.y != null) {
    return `Pin ${Number(item.x).toFixed(1)} m east, ${Number(item.y).toFixed(1)} m north.`;
  }
  return "No phone position yet. Stand at the object and press Save.";
}

function renderItems() {
  const items = (state.items || []).filter((item) => item.x != null && item.y != null);
  const empty = $("items-empty");
  empty.hidden = items.length > 0;
  empty.textContent = (state.items || []).length > items.length
    ? "No phone pin yet. Older saves are kept. Stand at the object and press Save."
    : "Nothing saved yet.";
  const finding = state.find ? state.find.item_id : null;
  const key = JSON.stringify([items, finding, selectedItem, state.body && state.body.frame_id]);
  if (key === itemsKey) return;
  itemsKey = key;
  const list = $("items");
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement("li");
    if (item.id === selectedItem) row.className = "selected";
    const picture = document.createElement("img");
    picture.alt = item.cover_keyframe ? "Last seen" : "";
    if (item.cover_keyframe) picture.src = keyframeUrl(item.cover_keyframe);
    else picture.hidden = true;
    const body = document.createElement("div");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = item.name;
    const line = document.createElement("div");
    line.className = "small";
    line.textContent = itemLine(item);
    const actions = document.createElement("div");
    actions.className = "actions";
    const add = (label, kind, handler, disabled) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      if (kind) button.className = kind;
      button.disabled = Boolean(disabled);
      button.addEventListener("click", handler);
      actions.append(button);
    };
    const canFind = sameFrame(item);
    add(
      finding === item.id ? "Finding…" : "Find",
      "",
      () => act(`/api/items/${item.id}/find`, item.id),
      finding === item.id || !canFind
    );
    add("Delete", "danger", () => {
      if (confirm(`Delete ${item.name} and its trail?`)) act(`/api/items/${item.id}/delete`);
    });
    body.append(name, line, actions);
    row.append(picture, body);
    list.append(row);
  }
}

async function act(path, selectId) {
  try {
    await post(path);
    if (selectId) selectedItem = selectId;
    banner($("items-msg"), "error", "", "");
  } catch (error) {
    banner($("items-msg"), "error", "That did not work", error.message);
  }
}

const saveTitles = {
  invalid_name: "Invalid item name",
  storage_error: "Not saved: filesystem error",
  too_many_items: "Not saved: too many items",
  busy: "Not saved: another Save is running",
};

function renderSave() {
  const link = state.link;
  const save = state.save;
  $("btn-save").disabled = Boolean(state.capture) || saveBusy;
  const box = $("save-status");
  if (state.capture && save) {
    banner(
      box,
      "wait",
      `${save.name} saved — collecting context (${save.seconds_left} s left)`,
      `${save.anchor_quality ? save.anchor_quality.toUpperCase() + " anchor. " : ""}` +
        `${save.references || 0} visual anchor frame(s), ${save.context || 0} context frame(s).`
    );
  } else if (saveNote) {
    banner(box, "error", saveTitles[saveNote.code] || "Not saved", saveNote.text);
  } else if (save && save.state === "failed") {
    banner(box, "error", saveTitles[save.code] || "Not saved", save.text);
  } else if (save && save.state === "saved") {
    banner(box, "ok", `${save.name} saved`, save.text);
  } else if (save && save.state === "capturing") {
    banner(box, "ok", `${save.name} saved`, save.text);
  } else {
    banner(box, "", "", "");
  }
  const hint = $("save-hint");
  hint.textContent = "";
}

function renderMotor() {
  const motor = state.motor;
  const box = $("motor-enabled");
  if (document.activeElement !== box) box.checked = motor.enabled;
  box.disabled = !motor.writes_allowed;
  $("btn-test-pulse").disabled = !motor.writes_allowed || !state.link.connected;
  let line = `Commands sent this run: ${motor.commands_sent}. Pulses the ring reported starting: ${motor.board_pulse_starts}, finishing: ${motor.board_pulse_ends}. ` +
    `Requests not sent: ${motor.requests_blocked}. Limits: at most one every ${motor.min_interval_s} s and ${motor.max_per_minute} per minute.`;
  if (motor.last_pulse_on_us) line += ` Last pulse measured by the ring: ${(motor.last_pulse_on_us / 1000).toFixed(1)} ms.`;
  if (!motor.writes_allowed) line = "Started with --no-motor: ReTrace writes nothing to the ring in this run. " + line;
  $("motor-line").textContent = line;
  const log = $("motor-log");
  log.replaceChildren();
  for (const entry of motor.log.slice(0, 6)) {
    const row = document.createElement("li");
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = clock(entry.unix_ms);
    row.append(when, `${entry.reason}: ${entry.sent ? "one pulse command sent" : "not sent, " + entry.blocked}` + (entry.detail ? ` (${entry.detail})` : ""));
    log.append(row);
  }
}

function renderRing() {
  const link = state.link;
  const board = link.board || {};
  $("ring-line").textContent = link.connected
    ? `Camera frames read: ${link.frames_valid} valid, ${link.frames_invalid} invalid. Reconnects: ${Math.max(0, link.connects - 1)}. ` +
      `Board restarts seen: ${link.board_restarts}.` + (board.up_ms ? ` Board up ${Math.round(board.up_ms / 1000)} s.` : "")
    : (link.error || "Ring not connected.");
  const imu = link.imu;
  $("imu-line").textContent = imu.last
    ? `IMU: acceleration ${imu.last.ax_g}, ${imu.last.ay_g}, ${imu.last.az_g} g; rotation ${imu.last.gx_dps}, ${imu.last.gy_dps}, ${imu.last.gz_dps} deg/s. ${imu.samples} samples logged.`
    : "IMU: no sample yet.";
  const memory = state.memory;
  const limits = memory.limits;
  $("memory-line").textContent =
    `Observations: ${memory.rolling_frames} of ${limits.frames} frames ` +
    `(${memory.rolling_landmarks || 0} landmarks of ${limits.landmarks}), ` +
    `${memory.rolling_mb} of ${limits.mb} MB, kept at most ${limits.minutes} min. ` +
    `Item pictures: ${memory.item_frames} frames, ${memory.item_mb} MB. IMU log: ${memory.imu_log_mb} of ${limits.imu_log_mb} MB.`;
  const names = {
    blurry: "blurry", few_features: "too little detail", obstructed: "camera obstructed", covered: "partly covered",
    duplicate: "same view", too_soon: "too soon", fast_motion: "moving too fast", dark: "too dark", bright: "too bright",
    undecodable: "unreadable", storage_error: "not written to disk",
  };
  const parts = Object.entries(state.frames.rejected).map(([key, count]) => `${names[key] || key} ${count}`);
  $("reject-line").textContent = parts.length ? "Frames not used as landmarks / skipped for observation: " + parts.join(", ") + "." : "";
  const usableRejects = state.frames.usable_rejects || {};
  const usableParts = Object.entries(usableRejects).map(([key, count]) => `${key} ${count}`);
  const embed = state.embedder || {};
  $("usable-line").textContent =
    `Usable frames this run: ${state.frames.usable || 0}. Bank keyframes kept: ${state.frames.bank_kept || 0}` +
    (memory.bank_keyframes != null ? ` (store ${memory.bank_keyframes})` : "") +
    `. Embedder: ${embed.ready ? embed.device : (embed.error || "loading/off")}` +
    (usableParts.length ? `. Usable rejects: ${usableParts.join(", ")}` : "") + ".";
  const put = state.putdown || {};
  const last = (put.events || [])[0];
  $("putdown-line").textContent = last
    ? `Detector state: ${put.state || "-"}. Last ${last.source} at ${clock(last.unix_ms)} (pinned ${last.pinned_frames || 0} frames).`
    : `Detector state: ${put.state || "idle"}. No put-down events yet.`;
}

function crumb(stage, keyframeId, classes, label) {
  const cell = document.createElement("div");
  cell.className = "crumb " + classes;
  cell.dataset.stage = String(stage);
  const picture = document.createElement("img");
  picture.loading = "lazy";
  picture.alt = "";
  picture.src = keyframeUrl(keyframeId);
  const caption = document.createElement("div");
  caption.textContent = label;
  cell.append(picture, caption);
  return cell;
}

async function loadDetail(itemId, key) {
  detailKey = key;
  try {
    const response = await fetch(`/api/items/${itemId}`);
    detail = response.ok ? await response.json() : null;
  } catch (error) {
    detail = null;
  }
  timelineKey = "";
  if (state) renderTimeline();
}

function renderTimeline() {
  const strip = $("timeline");
  const find = state.find;
  if (find) {
    $("timeline-title").textContent = `Breadcrumb timeline: ${find.name}`;
    const key = `find:${find.item_id}:${find.stages.length}`;
    if (key !== timelineKey) {
      timelineKey = key;
      strip.replaceChildren();
      find.stages.forEach((keyframeId, stage) => {
        if (!keyframeId) return;
        strip.append(crumb(stage, keyframeId, stage === 0 ? "spot" : "", stage === 0 ? "saved spot" : String(stage)));
      });
    }
    const visited = new Set(find.visited);
    let current = null;
    for (const cell of strip.children) {
      const stage = Number(cell.dataset.stage);
      const isCurrent = find.status !== "UNKNOWN" && stage === find.confirmed_stage;
      cell.classList.toggle("visited", visited.has(stage));
      cell.classList.toggle("current", isCurrent);
      if (isCurrent) current = cell;
    }
    if (current && current.dataset.shown !== "1") {
      for (const cell of strip.children) delete cell.dataset.shown;
      current.dataset.shown = "1";
      current.scrollIntoView({ block: "nearest", inline: "center" });
    }
    $("timeline-note").textContent =
      "Yellow: where the live view is confirmed now. Blue: confirmed earlier in this search. Green: the saved spot.";
    return;
  }
  const items = state.items;
  if (!items.some((item) => item.id === selectedItem)) selectedItem = items.length ? items[items.length - 1].id : null;
  const item = items.find((entry) => entry.id === selectedItem);
  if (!item) {
    $("timeline-title").textContent = "Breadcrumb timeline";
    strip.replaceChildren();
    timelineKey = "";
    $("timeline-note").textContent = "Save an item, then walk away: the pictures the ring keeps appear here in order.";
    return;
  }
  $("timeline-title").textContent = `Breadcrumb timeline: ${item.name}`;
  const wanted = `${item.id}:${item.references}:${item.breadcrumbs}:${item.context || 0}`;
  if (wanted !== detailKey) {
    loadDetail(item.id, wanted);
    return;
  }
  const key = "item:" + wanted;
  if (key === timelineKey || !detail) return;
  timelineKey = key;
  strip.replaceChildren();
  detail.references.forEach((keyframeId, index) => {
    strip.append(crumb(0, keyframeId, "spot", index === 0 ? "saved spot" : "spot"));
  });
  let gaps = 0;
  for (const frame of detail.breadcrumbs) {
    const gap = frame.gap_before_s > 0;
    if (gap) gaps += 1;
    strip.append(crumb(frame.stage, frame.keyframe_id, gap ? "gap" : "", `${frame.stage} · ${clock(frame.unix_ms)}`));
  }
  if (item.trail_state === "recording" && strip.lastChild) strip.lastChild.scrollIntoView({ block: "nearest", inline: "end" });
  $("timeline-note").textContent =
    `${detail.references.length} anchor picture(s), ${detail.context ? detail.context.length : 0} context, ` +
    `then ${detail.breadcrumbs.length} breadcrumb(s) in the order they were recorded. ` +
    `${detail.anchor_quality ? "Anchor: " + detail.anchor_quality + ". " : ""}` +
    (gaps ? ` ${gaps} gap(s) where the ring was disconnected: nothing was recorded there.` : "");
}

function renderRecent() {
  const recent = state.memory.recent || [];
  const key = recent.map((frame) => `${frame.id}:${frame.role || ""}`).join(",");
  if (key === recentKey) return;
  recentKey = key;
  const strip = $("recent");
  strip.replaceChildren();
  for (const frame of recent) {
    const classes = frame.role === "landmark" ? "landmark" : "";
    strip.append(crumb(-1, frame.id, classes, (frame.role === "landmark" ? "L " : "") + clock(frame.unix_ms)));
  }
}

function renderNotices() {
  const list = $("notices");
  list.replaceChildren();
  for (const notice of state.notices) {
    const row = document.createElement("li");
    row.className = notice.kind;
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = clock(notice.unix_ms);
    row.append(when, notice.text);
    list.append(row);
  }
}

function renderPdr() {
  const card = $("pdr-card");
  const pdr = state.pdr;
  if (!pdr) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const canvas = $("pdr-map");
  const ctx = canvas.getContext("2d");
  const path = pdr.path || [];
  const pins = state.pdr_items || [];
  const points = path.map((p) => [p[0], p[1]]);
  if (pdr.x != null && pdr.y != null) points.push([pdr.x, pdr.y]);
  for (const mark of pdr.marks || []) {
    if (mark && mark.x != null) points.push([mark.x, mark.y]);
  }
  for (const pin of pins) {
    if (pin.x != null) points.push([pin.x, pin.y]);
  }
  const gate = pdr.walking ? "open" : "closed";
  $("pdr-line").textContent =
    `Walking gate ${gate}, ${pdr.steps} steps, path ${pdr.path_m} m, position (${pdr.x}, ${pdr.y}) m, step ${pdr.step_length} m.`;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!points.length) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const [x, y] of points) {
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }
  const pad = 0.8;
  minX -= pad; maxX += pad; minY -= pad; maxY += pad;
  const scale = Math.min(canvas.width / Math.max(maxX - minX, 0.5), canvas.height / Math.max(maxY - minY, 0.5));
  const toX = (x) => (x - minX) * scale + 16;
  const toY = (y) => canvas.height - ((y - minY) * scale + 16);
  const color = { swing: "#2FBF71", carry: "#FFB020", still: "#8A94A6" };
  ctx.lineWidth = 2;
  for (let i = 1; i < path.length; i++) {
    ctx.strokeStyle = color[path[i][2]] || "#8A94A6";
    ctx.beginPath();
    ctx.moveTo(toX(path[i - 1][0]), toY(path[i - 1][1]));
    ctx.lineTo(toX(path[i][0]), toY(path[i][1]));
    ctx.stroke();
  }
  const last = path.length ? path[path.length - 1] : [pdr.x, pdr.y];
  if (last && last[0] != null) {
    ctx.fillStyle = "#4C8DF6";
    ctx.beginPath();
    ctx.arc(toX(last[0]), toY(last[1]), 6, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.fillStyle = "#FF5A4D";
  ctx.font = "12px Inter, system-ui, sans-serif";
  for (const pin of pins) {
    if (pin.x == null) continue;
    ctx.beginPath();
    ctx.arc(toX(pin.x), toY(pin.y), 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillText(pin.name || "", toX(pin.x) + 8, toY(pin.y) - 6);
  }
}

function renderBody() {
  const body = state.body;
  const live = $("body-live");
  if (!body) {
    live.textContent = "Waiting for the phone.";
    return;
  }
  const frameLine = $("body-frame");
  if (frameLine) {
    const id = body.frame_id || "—";
    const value = body.pocket_offset == null ? "—" : `${Number(body.pocket_offset).toFixed(1)}°`;
    const word = body.offset_state === "frozen" ? "frozen" : (body.offset_state || "unset");
    frameLine.textContent = `Session ${id}, offset ${value}, ${word}.`;
  }
  const heading = body.heading == null ? "—" : `${body.heading.toFixed(1)}°`;
  const offset = body.calibrated ? `${body.pocket_offset.toFixed(1)}°` : `${body.offset_steps} of ${body.offset_needed}`;
  const age = body.packet_age_s == null ? "no packets yet" : `last packet ${body.packet_age_s.toFixed(1)} s ago`;
  const rates = body.sample_rates || {};
  const rateText = Object.entries(rates).map(([name, value]) => `${name} ${Number(value).toFixed(0)}/s`).join(", ");
  const gaps = body.sample_gap_ms || {};
  const gapText = Object.entries(gaps).map(([name, value]) => `${name} ${Number(value).toFixed(1)} ms`).join(", ");
  const stall = body.stalls && body.stalls.length ? body.stalls[body.stalls.length - 1] : null;
  const source = body.heading_source ? ` from ${body.heading_source}` : "";
  live.textContent =
    `${body.packet_rate.toFixed(2)} packets/s${rateText ? ` (${rateText})` : ""}, heading ${heading}${source}, ` +
    `${body.steps} steps, step ${body.step_length.toFixed(2)} m, pocket offset ${offset}. ${age}.` +
    (gapText ? ` Median sample spacing: ${gapText}.` : "") +
    (stall ? ` ${stall.reason}` : body.packet_age_s > 1 ? " No samples are arriving." : "");
  const guide = $("body-guide");
  if (body.guide) {
    const pace = body.guide.interval_s == null ? "The ring is quiet." : `The ring buzzes every ${body.guide.interval_s.toFixed(1)} s.`;
    const turn = body.guide.error_deg > 1 ? "Turn right." : body.guide.error_deg < -1 ? "Turn left." : "You are facing it.";
    guide.textContent =
      `${body.guide.distance_m.toFixed(1)} m to ${body.guide.name}. ${turn} ${pace}`;
  } else if (body.packet_age_s == null || body.packet_age_s > 3) {
    guide.textContent = "The phone is not sending. Start the Sensor Logger recording, then Save.";
  } else {
    guide.textContent = "Stand at the object and press Save. Walk away, then press Find.";
  }
  const loop = $("body-loop");
  if (body.loop && body.loop.open) {
    loop.textContent = "Loop started. Walk back to the same spot and press Loop end.";
  } else if (body.loop && body.loop.closure_m != null) {
    loop.textContent =
      `Last loop: ${body.loop.steps} steps, path ${body.loop.path_m.toFixed(1)} m, ` +
      `closure error ${body.loop.closure_m.toFixed(1)} m.`;
  } else if (body.calibration_armed) {
    loop.textContent = `Calibration walk: ${body.steps_since_arm} steps so far. Enter the metres you walked.`;
  } else {
    loop.textContent = "";
  }
  drawBodyMap(body);
  drawHeadingDial(body);
}

const dialTrail = [];

function drawHeadingDial(body) {
  const canvas = $("heading-dial");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const radius = 72;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#263149";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "#8A94A6";
  ctx.font = "12px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const marks = [["N", 0], ["E", 90], ["S", 180], ["W", 270]];
  for (const [label, deg] of marks) {
    const radians = deg * Math.PI / 180;
    ctx.fillText(label, cx + Math.sin(radians) * (radius + 14), cy - Math.cos(radians) * (radius + 14));
  }
  const heading = body.heading;
  if (heading == null) {
    ctx.fillStyle = "#8A94A6";
    ctx.fillText("no heading", cx, cy);
    return;
  }
  const last = dialTrail.length ? dialTrail[dialTrail.length - 1] : null;
  const step = last == null ? 999 : Math.abs(((heading - last + 540) % 360) - 180);
  if (step > 0.4) {
    dialTrail.push(heading);
    if (dialTrail.length > 720) dialTrail.shift();
  }
  ctx.fillStyle = "#4C8DF6";
  for (const deg of dialTrail) {
    const radians = deg * Math.PI / 180;
    ctx.beginPath();
    ctx.arc(cx + Math.sin(radians) * (radius - 8), cy - Math.cos(radians) * (radius - 8), 2, 0, Math.PI * 2);
    ctx.fill();
  }
  const radians = heading * Math.PI / 180;
  ctx.strokeStyle = "#4C8DF6";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(cx - Math.sin(radians) * 16, cy + Math.cos(radians) * 16);
  ctx.lineTo(cx + Math.sin(radians) * (radius - 12), cy - Math.cos(radians) * (radius - 12));
  ctx.stroke();
  ctx.fillStyle = "#F2F4F8";
  ctx.font = "14px Inter, system-ui, sans-serif";
  ctx.fillText(`${heading.toFixed(0)}°`, cx, cy);
}

function drawBodyMap(body) {
  const canvas = $("body-map");
  const ctx = canvas.getContext("2d");
  const path = body.path || [];
  const pins = state.pdr_items || [];
  const points = path.map((point) => [point[0], point[1]]);
  for (const pin of pins) {
    if (pin.x != null) points.push([pin.x, pin.y]);
  }
  if (body.guide) points.push([body.guide.target_x, body.guide.target_y]);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!points.length) return;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const [x, y] of points) {
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }
  const pad = 0.8;
  minX -= pad; maxX += pad; minY -= pad; maxY += pad;
  const scale = Math.min(
    (canvas.width - 32) / Math.max(maxX - minX, 0.5),
    (canvas.height - 32) / Math.max(maxY - minY, 0.5),
  );
  const toX = (x) => (x - minX) * scale + 16;
  const toY = (y) => canvas.height - ((y - minY) * scale + 16);
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#8A94A6";
  ctx.beginPath();
  path.forEach((point, index) => {
    const x = toX(point[0]);
    const y = toY(point[1]);
    const broken = index > 0 && point[2] === 1;
    if (index === 0 || broken) {
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x, y);
      if (broken) {
        ctx.strokeStyle = "#FF5A4D";
        ctx.fillStyle = "#FF5A4D";
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
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
  if (body.guide && here) {
    ctx.strokeStyle = "#FFB020";
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(toX(here[0]), toY(here[1]));
    ctx.lineTo(toX(body.guide.target_x), toY(body.guide.target_y));
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#FF5A4D";
  ctx.font = "12px Inter, system-ui, sans-serif";
  for (const pin of pins) {
    if (pin.x == null) continue;
    ctx.beginPath();
    ctx.arc(toX(pin.x), toY(pin.y), 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillText(pin.name || "", toX(pin.x) + 8, toY(pin.y) - 6);
  }
  if (body.guide) {
    ctx.fillStyle = "#FFB020";
    ctx.font = "14px Inter, system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(
      `${Number(body.guide.distance_m).toFixed(1)} m    bearing ${Number(body.guide.bearing).toFixed(0)}°`,
      16,
      22,
    );
  }
  if (here && here[0] != null) {
    const x = toX(here[0]);
    const y = toY(here[1]);
    ctx.fillStyle = "#4C8DF6";
    ctx.beginPath();
    ctx.arc(x, y, 6, 0, Math.PI * 2);
    ctx.fill();
    if (body.heading != null) {
      const radians = body.heading * Math.PI / 180;
      const tipX = x + Math.sin(radians) * 22;
      const tipY = y - Math.cos(radians) * 22;
      ctx.strokeStyle = "#4C8DF6";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(tipX, tipY);
      ctx.stroke();
    }
  }
}

function render() {
  if (!state) return;
  renderMode();
  renderHeader();
  renderTracking();
  renderBody();
  renderPdr();
  renderItems();
  renderSave();
  renderMotor();
  renderRing();
  renderTimeline();
  renderRecent();
  renderNotices();
}

$("save-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (saveBusy) return;
  const name = $("save-name").value;
  saveNote = null;
  saveBusy = true;
  if (state) renderSave();
  try {
    const saved = await post("/api/items", { name });
    selectedItem = saved.item_id;
    $("save-name").value = "";
  } catch (error) {
    saveNote = { code: error.code || "", text: error.message };
  }
  saveBusy = false;
  if (state) renderSave();
  else banner($("save-status"), "error", "Not saved", saveNote ? saveNote.text : "");
});

$("btn-stop-find").addEventListener("click", () => act("/api/find/stop"));
$("btn-body-recalibrate").addEventListener("click", () => act("/api/body/recalibrate"));

$("motor-enabled").addEventListener("change", async (event) => {
  try {
    await post("/api/motor/enabled", { enabled: event.target.checked });
  } catch (error) {
    $("motor-line").textContent = error.message;
  }
  event.target.blur();
});

$("btn-test-pulse").addEventListener("click", async () => {
  try {
    const result = await post("/api/motor/test");
    if (!result.sent) $("motor-line").textContent = `Test pulse not sent: ${result.blocked}.`;
  } catch (error) {
    $("motor-line").textContent = error.message;
  }
});

async function markPutDown() {
  try {
    const result = await post("/api/putdown/mark", {});
    $("putdown-line").textContent = `Marked put-down at ${clock(result.unix_ms)}.`;
  } catch (error) {
    $("putdown-line").textContent = error.message;
  }
}

$("btn-mark-putdown").addEventListener("click", () => markPutDown());

$("btn-body-export").addEventListener("click", () => {
  const canvas = $("body-map");
  const link = document.createElement("a");
  link.href = canvas.toDataURL("image/png");
  link.download = "retrace-body-map.png";
  link.click();
});

$("btn-body-replay").addEventListener("click", async () => {
  const note = $("body-loop");
  try {
    for (const [path, filename] of [["/api/find/replay.png", "find-replay.png"], ["/api/find/replay.gif", "find-replay.gif"]]) {
      const response = await fetch(path, { headers: { "x-retrace": "1" } });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "No Find replay yet.");
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      URL.revokeObjectURL(link.href);
    }
    note.textContent = "Saved the Find replay as an image and a short animation.";
  } catch (error) {
    note.textContent = error.message;
  }
});

$("btn-body-arm").addEventListener("click", async () => {
  try {
    await post("/api/body/calibrate/start", {});
  } catch (error) {
    $("body-loop").textContent = error.message;
  }
});

$("btn-body-calibrate").addEventListener("click", async () => {
  try {
    const metres = Number($("body-metres").value);
    const result = await post("/api/body/calibrate", { metres });
    $("body-loop").textContent = `Step length is now ${result.step_length.toFixed(2)} m.`;
  } catch (error) {
    $("body-loop").textContent = error.message;
  }
});

$("btn-loop-start").addEventListener("click", async () => {
  try {
    await post("/api/body/loop/start", {});
  } catch (error) {
    $("body-loop").textContent = error.message;
  }
});

$("btn-loop-end").addEventListener("click", async () => {
  try {
    const report = await post("/api/body/loop/end", {});
    $("body-loop").textContent =
      `Loop: ${report.steps} steps, path ${report.path_m.toFixed(1)} m, closure ${report.closure_m.toFixed(1)} m.`;
  } catch (error) {
    $("body-loop").textContent = error.message;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  const tag = (event.target && event.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || event.target.isContentEditable) return;
  if (event.key === "p" || event.key === "P") {
    event.preventDefault();
    const name = $("save-name").value.trim();
    if (!name) {
      saveNote = { code: "invalid_name", text: "Type a name, then press P." };
      if (state) renderSave();
      return;
    }
    $("save-form").requestSubmit();
  }
});

connect();
