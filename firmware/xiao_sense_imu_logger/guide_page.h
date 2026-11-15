#pragma once
static const char GUIDE_HTML[] =
R"RETRACE(
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>ReTrace pocket</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101418;
      --card: #1a212b;
      --text: #e8eef6;
      --muted: #93a0b0;
      --line: #2c3848;
      --ok: #b6f27c;
      --warn: #f3e2b0;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 18px/1.4 "Segoe UI", system-ui, sans-serif;
      padding: 20px 16px calc(24px + env(safe-area-inset-bottom));
    }
    main { max-width: 520px; margin: 0 auto; }
    .eyebrow { margin: 0; color: var(--muted); font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; }
    h1 { margin: 4px 0 8px; font-size: 28px; }
    p { margin: 8px 0; }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      margin: 12px 0;
    }
    .row { display: flex; gap: 16px; }
    .stat { flex: 1; }
    .stat span { display: block; color: var(--muted); font-size: 13px; }
    .stat strong { font-size: 22px; font-variant-numeric: tabular-nums; }
    .row.four { gap: 8px; }
    #word {
      margin: 0;
      font-size: clamp(40px, 12vw, 64px);
      line-height: 1;
      letter-spacing: -0.03em;
    }
    #word[data-facing="yes"] { color: var(--ok); }
    #word[data-arrived="yes"] { color: var(--ok); }
    #away { margin: 8px 0 0; font-size: 28px; }
    label { display: block; color: var(--muted); font-size: 14px; margin-bottom: 6px; }
    input[type="text"], input[type="range"] {
      width: 100%;
      font: inherit;
      color: var(--text);
      background: #0d1218;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
    }
    button {
      width: 100%;
      margin-top: 10px;
      font: 600 18px/1 system-ui, sans-serif;
      color: #101418;
      background: var(--ok);
      border: 0;
      border-radius: 14px;
      padding: 16px;
    }
    button.quiet {
      color: var(--text);
      background: transparent;
      border: 1px solid var(--line);
    }
    .check { display: flex; align-items: center; gap: 8px; color: var(--text); margin-top: 12px; }
    .check input { width: auto; }
    .small { color: var(--muted); font-size: 14px; }
    .rose { margin: 4px 0 8px; }
    .rose svg {
      display: block;
      width: min(74vw, 280px);
      height: auto;
      margin: 0 auto;
    }
    .rose-face { fill: #0d1218; stroke: var(--line); stroke-width: 3; }
    .rose-ticks { fill: none; stroke: #93a0b0; stroke-width: 2; }
    .rose-letters { fill: #93a0b0; font: 700 15px/1 system-ui, sans-serif; }
    #rose-phone { fill: var(--text); }
    #rose-mark { color: var(--warn); }
    #rose-mark line { stroke: currentColor; stroke-width: 3; }
    #rose-mark circle { fill: currentColor; }
    #rose-mark[data-facing="yes"] { color: var(--ok); }
    .rose-hub { fill: var(--text); }
    #rose-caption { text-align: center; margin: 8px 0 0; }
    details { margin-top: 8px; }
    summary { cursor: pointer; color: var(--muted); }
  </style>
</head>
<body>
  <main>
    <p class="eyebrow">ReTrace</p>
    <h1>Direction guide</h1>
    <p class="small">Hold the phone upright, screen facing you, as if you are reading it. The back of the phone points the way you are looking. Flat, screen up, camera end pointing that same way, is the same heading. Do not turn the screen face down. Walking does not move the mark.</p>

    <section class="card">
      <div id="rose" class="rose">
        <svg viewBox="0 0 200 200" aria-hidden="true">
          <circle cx="100" cy="100" r="64" class="rose-face"></circle>
          <g class="rose-ticks">
            <path d="M100 36 L100 48"></path>
            <path d="M100 152 L100 164"></path>
            <path d="M36 100 L48 100"></path>
            <path d="M152 100 L164 100"></path>
            <path d="M100 36 L100 46" transform="rotate(45 100 100)"></path>
            <path d="M100 36 L100 46" transform="rotate(135 100 100)"></path>
            <path d="M100 36 L100 46" transform="rotate(225 100 100)"></path>
            <path d="M100 36 L100 46" transform="rotate(315 100 100)"></path>
          </g>
          <g class="rose-letters">
            <text x="100" y="24" text-anchor="middle">N</text>
            <text x="184" y="105" text-anchor="middle">E</text>
            <text x="100" y="186" text-anchor="middle">S</text>
            <text x="16" y="105" text-anchor="middle">W</text>
          </g>
          <g id="rose-mark" hidden>
            <line x1="100" y1="100" x2="100" y2="48"></line>
            <circle cx="100" cy="36" r="8"></circle>
          </g>
          <g id="rose-phone">
            <polygon points="100,52 112,98 100,86 88,98"></polygon>
          </g>
          <circle cx="100" cy="100" r="4" class="rose-hub"></circle>
        </svg>
        <p id="rose-caption" class="small">The arrow is the camera end. The dot is the marked direction.</p>
      </div>
      <div class="row four">
        <div class="stat"><span>Raw</span><strong id="raw-heading">—</strong></div>
        <div class="stat"><span>Tilt-safe</span><strong id="heading">0°</strong></div>
        <div class="stat"><span>Target</span><strong id="marked">—</strong></div>
        <div class="stat"><span>Difference</span><strong id="diff">—</strong></div>
      </div>
      <p id="compass-line" class="small">Tap enable. Safari asks for compass access only after that tap.</p>
      <button id="enable" type="button">Enable compass</button>
    </section>

    <section class="card">
      <label for="name">Name, if you want one</label>
      <input id="name" type="text" maxlength="40" placeholder="Corner" autocomplete="off">
      <button id="mark" type="button">Mark this direction</button>
      <p id="mark-line" class="small"></p>
    </section>

    <section class="card" aria-live="polite">
      <p id="word">Ready</p>
      <p id="away">—</p>
      <p id="hint" class="small">Point the camera end, then mark that direction.</p>
      <p id="ring-line" class="small">The ring pulses when the camera end points the way you marked.</p>
      <label class="check"><input id="beep" type="checkbox" checked> Beep when you are facing it</label>
      <button id="guide" type="button">Point me that way</button>
      <button id="pulse-once" class="quiet" type="button">Pulse the ring once</button>
      <button id="stop" class="quiet" type="button" hidden>Stop guide</button>
      <button id="reset" class="quiet" type="button">Start over</button>
    </section>

    <details>
      <summary>Practice without a compass</summary>
      <div class="card">
        <label for="practice-heading">Heading <span id="practice-value"></span></label>
        <input id="practice-heading" type="range" min="0" max="359" value="0">
        <p class="small">A real compass takes over once Safari provides it. Straight means within 25° of the marked direction. A compass reading worse than ±40° is ignored. A mark is stored only once the compass has settled: accuracy ±25° or better, and the recent readings within 15° of their average. Until then the page says the compass is still settling. Difference is target minus tilt-safe heading, positive to the right.</p>
      </div>
    </details>
  </main>
  <script src="/pocket.js"></script>
  <script>
    const range = document.getElementById("practice-heading");
    const value = document.getElementById("practice-value");
    const show = function () { value.textContent = range.value + "°"; };
    range.addEventListener("input", show);
    show();
  </script>
</body>
</html>
)RETRACE";
static const char GUIDE_JS[] =
R"RETRACE(
/* Direction guide for the iPhone compass.
   A mark stores the heading of the camera end once the compass has settled.
   Readings worse than ACCURACY_MAX are ignored. A mark also waits until
   accuracy is MARK_ACCURACY_MAX or better and the recent headings sit within
   MARK_SPREAD_MAX of their circular mean. That refuses the first, swinging
   samples. It does not shift the heading by a fixed offset.
   Guidance turns you until the camera end points the marked way again.
   Walking does not move the mark.
   webkitCompassAccuracy and the phone's tilt decide whether a reading is trusted. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document !== "undefined") {
    const start = function () { api.mount(document); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }
  root.Pocket = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  const FACE_DEG = 25;
  const ACCURACY_MAX = 40;
  const MARK_ACCURACY_MAX = 25;
  const MARK_SPREAD_MAX = 15;
  const TILT_MAX = 50;
  const RECENT_MAX = 8;
  const RECENT_MIN = 3;
  const BAD_STREAK = 3;

  function normDeg(deg) {
    return ((deg % 360) + 360) % 360;
  }

  // Signed shortest turn from heading to target. Positive means target is clockwise (turn right).
  // The +360 extra modulo is only so JavaScript's negative remainder still matches this formula.
  function signedDiff(target, heading) {
    return ((target - heading + 540) % 360 + 360) % 360 - 180;
  }

  function wrapDeg(deg) {
    return signedDiff(deg, 0);
  }

  // W3C rotation: device axes expressed in an east, north, up frame.
  // +Y is the top edge (camera end). +Z comes out of the screen. The back is -Z.
  function deviceAxes(alpha, beta, gamma) {
    const rad = Math.PI / 180;
    const cX = Math.cos(beta * rad);
    const sX = Math.sin(beta * rad);
    const cY = Math.cos(gamma * rad);
    const sY = Math.sin(gamma * rad);
    const cZ = Math.cos(alpha * rad);
    const sZ = Math.sin(alpha * rad);
    return {
      top: [-cX * sZ, cZ * cX, sX],
      screen: [cY * sZ * sX + cZ * sY, sZ * sY - cZ * cY * sX, cX * cY],
    };
  }

  function compassOf(east, north) {
    const horizontal = Math.hypot(east, north);
    if (horizontal < 1e-3) return null;
    return {
      deg: normDeg((Math.atan2(east, north) * 180) / Math.PI),
      horizontal: horizontal,
      east: east,
      north: north,
    };
  }

  // Forward is the back of the phone, so tipping the screen up past vertical does not reverse it.
  // While the phone is flat the back has no horizontal direction, and the top edge is used instead.
  // A back vector that points opposite the top edge is ignored while the screen still faces upward.
  function poseHeadings(alpha, beta, gamma) {
    const axes = deviceAxes(alpha, beta, gamma);
    const top = compassOf(axes.top[0], axes.top[1]);
    const back = compassOf(-axes.screen[0], -axes.screen[1]);
    const screenUp = axes.screen[2];
    const topWeight = Math.max(0, screenUp);
    let backWeight = back ? back.horizontal : 0;
    if (top && back && topWeight > 0.2) {
      const dot = axes.top[0] * -axes.screen[0] + axes.top[1] * -axes.screen[1];
      if (dot < 0) backWeight = 0;
    }
    const east = (top ? top.east * topWeight : 0) + (back ? back.east * backWeight : 0);
    const north = (top ? top.north * topWeight : 0) + (back ? back.north * backWeight : 0);
    return {
      top: top,
      forward: compassOf(east, north),
    };
  }

  function circularMean(degs) {
    if (!degs || !degs.length) return null;
    let sinSum = 0;
    let cosSum = 0;
    for (let i = 0; i < degs.length; i += 1) {
      const rad = (degs[i] * Math.PI) / 180;
      sinSum += Math.sin(rad);
      cosSum += Math.cos(rad);
    }
    if (Math.hypot(sinSum, cosSum) < 1e-6) return null;
    return (Math.atan2(sinSum, cosSum) * 180 / Math.PI + 360) % 360;
  }

  function headingSpread(degs) {
    const mean = circularMean(degs);
    if (mean == null) return null;
    let spread = 0;
    for (let i = 0; i < degs.length; i += 1) {
      const off = Math.abs(wrapDeg(degs[i] - mean));
      if (off > spread) spread = off;
    }
    return spread;
  }

  function accuracyGoodForMark(accuracy) {
    if (accuracy == null) return true;
    if (accuracy < 0) return false;
    return accuracy <= MARK_ACCURACY_MAX;
  }

  function sampleUsable(sample) {
    if (!sample || typeof sample.heading !== "number" || Number.isNaN(sample.heading)) return false;
    if (sample.accuracy == null) return true;
    if (sample.accuracy < 0) return false;
    return sample.accuracy <= ACCURACY_MAX;
  }

  function tiltOk(sample) {
    if (!sample || sample.beta == null) return true;
    // Face down has no stable forward direction. Upright, screen facing you, is a normal hold.
    return Math.abs(sample.beta) <= 150;
  }

  function guidance(state) {
    const marker = state.marker;
    if (!marker || typeof marker.heading !== "number") {
      return {
        word: "No mark",
        hint: "Point the camera end, then mark that direction.",
        errorDeg: null,
        facing: false,
      };
    }
    if (state.upsideDown) {
      return {
        word: "Turn around",
        hint: "The camera end is toward you. Point that end where you look.",
        errorDeg: null,
        facing: false,
      };
    }
    if (state.tiltBad) {
      return {
        word: "Lay flat",
        hint: "Screen up. Point the camera end where you are looking.",
        errorDeg: null,
        facing: false,
      };
    }
    if (state.compassBad) {
      return {
        word: "Unsteady",
        hint: "Wave the phone in a figure-8. The compass is unsure.",
        errorDeg: null,
        facing: false,
      };
    }
    const error = signedDiff(marker.heading, state.heading);
    const facing = Math.abs(error) <= (state.faceDeg == null ? FACE_DEG : state.faceDeg);
    if (facing) {
      return {
        word: "Straight",
        hint: "This is the direction you marked.",
        errorDeg: error,
        facing: true,
        bearing: marker.heading,
      };
    }
    return {
      word: error > 0 ? "Turn right" : "Turn left",
      hint: "Turn until the camera end points the way you marked.",
      errorDeg: error,
      facing: false,
      bearing: marker.heading,
    };
  }

  function smoothHeading(prev, next, accuracy) {
    if (prev == null || Number.isNaN(prev)) return normDeg(next);
    let nextWeight = 0.35;
    if (typeof accuracy === "number" && accuracy >= 0) {
      nextWeight = Math.max(0.08, 0.55 - accuracy / 80);
    }
    const prevWeight = 1 - nextWeight;
    const prevRad = (normDeg(prev) * Math.PI) / 180;
    const nextRad = (normDeg(next) * Math.PI) / 180;
    const sinSum = Math.sin(prevRad) * prevWeight + Math.sin(nextRad) * nextWeight;
    const cosSum = Math.cos(prevRad) * prevWeight + Math.cos(nextRad) * nextWeight;
    if (Math.hypot(sinSum, cosSum) < 1e-6) return normDeg(next);
    return normDeg((Math.atan2(sinSum, cosSum) * 180) / Math.PI);
  }

  function HeadingFix() {
    this.offset = 0;
    this.haveOffset = false;
    this.lastTilt = null;
  }

  HeadingFix.prototype.read = function (event) {
    const webkit = event && typeof event.webkitCompassHeading === "number" && !Number.isNaN(event.webkitCompassHeading)
      ? normDeg(event.webkitCompassHeading)
      : null;
    const hasAlpha = event && typeof event.alpha === "number" && !Number.isNaN(event.alpha);
    const beta = event && typeof event.beta === "number" && !Number.isNaN(event.beta) ? event.beta : null;
    const gamma = event && typeof event.gamma === "number" && !Number.isNaN(event.gamma) ? event.gamma : null;
    // iOS reports webkitCompassHeading. Alpha is only the pose, not the compass, when that value is present.
    let raw = webkit;
    if (raw == null && event && event.absolute === true && hasAlpha) raw = normDeg(360 - event.alpha);
    if (raw == null) return null;

    let tiltSafe = raw;
    if (hasAlpha && beta != null && gamma != null) {
      const pose = poseHeadings(event.alpha, beta, gamma);
      const topReliable = !!(pose.top && pose.top.horizontal >= 0.4);
      if (webkit != null && topReliable) {
        this.offset = signedDiff(webkit, pose.top.deg);
        this.haveOffset = true;
      }
      if (pose.forward) {
        if (webkit != null && this.haveOffset) tiltSafe = normDeg(pose.forward.deg + this.offset);
        else if (webkit == null) tiltSafe = pose.forward.deg;
        this.lastTilt = tiltSafe;
      } else if (this.lastTilt != null) {
        tiltSafe = this.lastTilt;
      }
    }
    return {
      raw: raw,
      tiltSafe: tiltSafe,
      accuracy: event && typeof event.webkitCompassAccuracy === "number" && !Number.isNaN(event.webkitCompassAccuracy)
        ? event.webkitCompassAccuracy
        : null,
      beta: beta,
      gamma: gamma,
    };
  };

  function headingFromEvent(event) {
    if (event && typeof event.webkitCompassHeading === "number" && !Number.isNaN(event.webkitCompassHeading)) {
      return (event.webkitCompassHeading + 360) % 360;
    }
    if (event && event.absolute === true && typeof event.alpha === "number" && !Number.isNaN(event.alpha)) {
      return (360 - event.alpha) % 360;
    }
    return null;
  }

  function sampleFromEvent(event) {
    const heading = headingFromEvent(event);
    if (heading == null) return null;
    const accuracy = event && typeof event.webkitCompassAccuracy === "number" && !Number.isNaN(event.webkitCompassAccuracy)
      ? event.webkitCompassAccuracy
      : null;
    const beta = event && typeof event.beta === "number" && !Number.isNaN(event.beta) ? event.beta : null;
    const gamma = event && typeof event.gamma === "number" && !Number.isNaN(event.gamma) ? event.gamma : null;
    return { heading: heading, accuracy: accuracy, beta: beta, gamma: gamma };
  }

  function Walk() {
    this.heading = 0;
    this.rawHeading = null;
    this.marker = null;
    this.recent = [];
    this.accuracy = null;
    this.beta = null;
    this.gamma = null;
    this.compassBad = false;
    this.tiltBad = false;
    this.upsideDown = false;
    this.settling = false;
    this.badStreak = 0;
  }

  Walk.prototype.setHeading = function (deg) {
    this.heading = ((deg % 360) + 360) % 360;
  };

  Walk.prototype.noteSample = function (sample) {
    if (!sample) return;
    this.accuracy = sample.accuracy;
    this.beta = sample.beta;
    this.gamma = sample.gamma;
    const bad = !sampleUsable(sample);
    const tilt = !tiltOk(sample);
    const flipped = !!sample.upsideDown;
    if (!bad && !tilt && !flipped && typeof sample.heading === "number") {
      this.badStreak = 0;
      this.compassBad = false;
      this.tiltBad = false;
      this.upsideDown = false;
      this.recent.push(sample.heading);
      if (this.recent.length > RECENT_MAX) this.recent.shift();
      return;
    }
    this.badStreak += 1;
    if (this.badStreak < BAD_STREAK) return;
    this.upsideDown = flipped;
    this.tiltBad = tilt && !flipped;
    this.compassBad = bad && !tilt && !flipped;
  };

  Walk.prototype.settled = function () {
    if (!accuracyGoodForMark(this.accuracy)) return false;
    if (this.recent.length < RECENT_MIN) return false;
    const spread = headingSpread(this.recent);
    return spread != null && spread <= MARK_SPREAD_MAX + 1e-6;
  };

  Walk.prototype.mark = function (name) {
    if (this.upsideDown) return null;
    let heading = null;
    if (this.recent.length >= RECENT_MIN) {
      if (!this.settled()) {
        this.settling = true;
        return null;
      }
      this.settling = false;
      heading = circularMean(this.recent);
    } else if (this.accuracy == null && !this.compassBad && !this.tiltBad) {
      this.settling = false;
      heading = this.heading;
    } else {
      this.settling = !accuracyGoodForMark(this.accuracy);
    }
    if (heading == null || Number.isNaN(heading)) return null;
    this.marker = {
      name: (name || "").trim() || "Direction",
      heading: (heading + 360) % 360,
      accuracy: this.accuracy,
    };
    return this.marker;
  };

  Walk.prototype.reset = function () {
    this.heading = 0;
    this.rawHeading = null;
    this.marker = null;
    this.recent = [];
    this.accuracy = null;
    this.beta = null;
    this.gamma = null;
    this.compassBad = false;
    this.tiltBad = false;
    this.upsideDown = false;
    this.settling = false;
    this.badStreak = 0;
  };

  function markRefusal(walk) {
    if (walk.upsideDown) return "Turn the phone so the camera end points away from you, then mark.";
    if (walk.tiltBad) return "Lay the phone flat, screen up, then mark.";
    if (walk.compassBad) return "Compass is unsure. Wave the phone in a figure-8, then mark.";
    if (walk.settling) return "The compass is still settling. Hold the phone still, or wave it in a figure-8, then mark.";
    return "Hold the phone still a moment, then mark.";
  }

  Walk.prototype.guide = function () {
    return guidance({
      heading: this.heading,
      marker: this.marker,
      compassBad: this.compassBad,
      tiltBad: this.tiltBad,
      upsideDown: this.upsideDown,
    });
  };

  function mount(doc) {
    const walk = new Walk();
    const word = doc.getElementById("word");
    const hint = doc.getElementById("hint");
    const away = doc.getElementById("away");
    const headingEl = doc.getElementById("heading");
    const markedEl = doc.getElementById("marked");
    const markLine = doc.getElementById("mark-line");
    const compassLine = doc.getElementById("compass-line");
    const enable = doc.getElementById("enable");
    const markBtn = doc.getElementById("mark");
    const guideBtn = doc.getElementById("guide");
    const stopBtn = doc.getElementById("stop");
    const resetBtn = doc.getElementById("reset");
    const nameInput = doc.getElementById("name");
    const beepBox = doc.getElementById("beep");
    const practiceHeading = doc.getElementById("practice-heading");
    const rosePhone = doc.getElementById("rose-phone");
    const roseMark = doc.getElementById("rose-mark");
    const roseCaption = doc.getElementById("rose-caption");
    if (!word || !walk) return;

    let guiding = false;
    let compass = false;
    let smoothed = null;
    const headingFix = new HeadingFix();
    let audio = null;
    let guideTimer = null;
    let lastPulseMs = 0;
    const ringLine = doc.getElementById("ring-line");
    const pulseOnce = doc.getElementById("pulse-once");
    const storeKey = "retrace-pocket-direction";

    function signedText(diff) {
      const n = Math.round(diff);
      if (n > 0) return "+" + n + "°";
      return n + "°";
    }

    function offText(g) {
      if (!g || g.errorDeg == null) return "—";
      if (g.facing) return "On the mark";
      const n = Math.abs(Math.round(g.errorDeg));
      return n + "° " + (g.errorDeg > 0 ? "to the right" : "to the left");
    }

    function compassStatus() {
      if (walk.upsideDown) return "The camera end is toward you. Turn the phone around.";
      if (walk.tiltBad) return "The phone is too upright. Lay it flatter, screen up.";
      if (walk.compassBad) {
        const shown = walk.accuracy == null ? "" : " (±" + Math.round(Math.abs(walk.accuracy)) + "°)";
        return "Compass is unsure" + shown + ". Wave the phone in a figure-8.";
      }
      if (walk.accuracy != null && !walk.settled()) {
        return "The compass is still settling. Hold still, or wave the phone in a figure-8.";
      }
      if (walk.accuracy == null) return "Compass is live. Screen facing you, back of the phone pointing where you look.";
      return "Compass ±" + Math.round(walk.accuracy) + "°. Screen facing you, back of the phone pointing where you look.";
    }

    function paint() {
      const g = walk.guide();
      word.textContent = guiding || walk.marker ? g.word : "Ready";
      if (!walk.marker) hint.textContent = "Point the camera end, then mark that direction.";
      else if (!guiding) hint.textContent = "Marked " + Math.round(walk.marker.heading) + "°. Start the guide, then turn until it says straight.";
      else hint.textContent = g.hint;
      away.textContent = offText(g);
      const rawEl = doc.getElementById("raw-heading");
      const diffEl = doc.getElementById("diff");
      if (rawEl) rawEl.textContent = walk.rawHeading == null ? "—" : Math.round(walk.rawHeading) + "°";
      headingEl.textContent = Math.round(walk.heading) + "°";
      if (markedEl) markedEl.textContent = walk.marker ? Math.round(walk.marker.heading) + "°" : "—";
      if (diffEl) {
        diffEl.textContent = walk.marker ? signedText(signedDiff(walk.marker.heading, walk.heading)) : "—";
      }
      word.dataset.facing = g.facing ? "yes" : "no";
      if (compass) compassLine.textContent = compassStatus();
      paintRose();
    }

    function paintRose() {
      if (!rosePhone) return;
      const heading = walk.heading;
      rosePhone.setAttribute("transform", "rotate(" + heading + " 100 100)");
      if (!roseMark) return;
      if (!walk.marker || typeof walk.marker.heading !== "number") {
        roseMark.setAttribute("hidden", "");
        roseMark.removeAttribute("data-facing");
        if (roseCaption) roseCaption.textContent = "Arrow " + Math.round(heading) + "° from north. That is the camera end. Mark a direction to place the dot.";
        return;
      }
      const marked = walk.marker.heading;
      const error = signedDiff(marked, heading);
      const facing = Math.abs(error) <= FACE_DEG;
      roseMark.removeAttribute("hidden");
      roseMark.setAttribute("transform", "rotate(" + marked + " 100 100)");
      roseMark.setAttribute("data-facing", facing ? "yes" : "no");
      if (!roseCaption) return;
      const relation = facing
        ? "The arrow lines up with the marked direction."
        : "The marked direction is " + Math.abs(Math.round(error)) + "° " + (error > 0 ? "to the right." : "to the left.");
      roseCaption.textContent = "Arrow " + Math.round(heading) + "°. Dot " + Math.round(marked) + "°. " + relation;
    }

    function setHeading(deg, fromCompass, accuracy) {
      if (fromCompass) {
        smoothed = smoothHeading(smoothed, deg, accuracy);
        walk.setHeading(smoothed);
      } else {
        smoothed = deg;
        walk.setHeading(deg);
      }
      paint();
    }

    function loadMarker() {
      try {
        const saved = JSON.parse(root.localStorage.getItem(storeKey) || "null");
        if (saved && typeof saved.heading === "number") {
          walk.marker = {
            name: saved.name || "Direction",
            heading: (saved.heading + 360) % 360,
          };
          markLine.textContent = "Last direction: " + walk.marker.name + " at " + Math.round(walk.marker.heading) + "°.";
        }
      } catch (error) {
        markLine.textContent = "";
      }
    }

    function pulseRing() {
      const nowMs = Date.now();
      if (nowMs - lastPulseMs < 280) return;
      lastPulseMs = nowMs;
      fetch("/pulse", { method: "POST" })
        .then(function (res) { return res.json().then(function (body) { return { status: res.status, body: body }; }); })
        .then(function (result) {
          ringLine.textContent = result.body.ok ? "Ring pulsed." : "Ring did not pulse. " + (result.body.error || "");
        })
        .catch(function () {
          ringLine.textContent = "The phone could not reach the ring, so it did not pulse.";
        });
    }

    function guideTick() {
      if (guideTimer) {
        clearTimeout(guideTimer);
        guideTimer = null;
      }
      if (!guiding) return;
      const g = walk.guide();
      let gap = 300;
      if (g.facing) {
        const off = Math.min(FACE_DEG, Math.abs(g.errorDeg || 0));
        gap = 320 + (off / FACE_DEG) * 400;
        pulseRing();
        if (beepBox.checked && audio) {
          const now = audio.currentTime;
          const osc = audio.createOscillator();
          const gain = audio.createGain();
          osc.frequency.value = 520 + (1 - off / FACE_DEG) * 180;
          gain.gain.setValueAtTime(0.0001, now);
          gain.gain.exponentialRampToValueAtTime(0.08, now + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.09);
          osc.connect(gain);
          gain.connect(audio.destination);
          osc.start(now);
          osc.stop(now + 0.1);
        }
      }
      guideTimer = setTimeout(guideTick, gap);
    }

    function ensureAudio() {
      const Ctx = root.AudioContext || root.webkitAudioContext;
      if (!Ctx) return;
      if (!audio) audio = new Ctx();
      if (audio.state === "suspended") audio.resume();
    }

    function phoneUpsideDown() {
      const angle = typeof root.orientation === "number" ? root.orientation : null;
      if (angle === 180 || angle === -180) return true;
      const screenAngle = root.screen && root.screen.orientation ? root.screen.orientation.angle : null;
      return screenAngle === 180;
    }

    function takeSample(event) {
      const reading = headingFix.read(event);
      if (!reading) return;
      const sample = {
        heading: reading.tiltSafe,
        accuracy: reading.accuracy,
        beta: reading.beta,
        gamma: reading.gamma,
        upsideDown: phoneUpsideDown(),
      };
      compass = true;
      enable.hidden = true;
      walk.rawHeading = reading.raw;
      walk.noteSample(sample);
      if (sampleUsable(sample) && tiltOk(sample) && !sample.upsideDown) {
        setHeading(reading.tiltSafe, true, reading.accuracy);
      } else {
        paint();
      }
    }

    enable.addEventListener("click", function () {
      ensureAudio();
      const jobs = [];
      if (root.DeviceOrientationEvent && typeof root.DeviceOrientationEvent.requestPermission === "function") {
        jobs.push(root.DeviceOrientationEvent.requestPermission());
      }
      Promise.all(jobs).then(function (answers) {
        const denied = answers.some(function (answer) { return answer && answer !== "granted"; });
        compassLine.textContent = denied
          ? "Compass permission was denied. Turn it on in Settings, Safari, Motion & Orientation."
          : "Waiting for the compass. Hold the phone upright, screen facing you.";
        enable.hidden = denied;
      }).catch(function () {
        compassLine.textContent = "This page could not ask for the compass. On the iPhone, open the https address.";
      });
    });

    root.addEventListener("deviceorientation", takeSample, true);
    root.addEventListener("deviceorientationabsolute", takeSample, true);

    markBtn.addEventListener("click", function () {
      ensureAudio();
      const marker = walk.mark(nameInput.value);
      if (!marker) {
        markLine.textContent = markRefusal(walk);
        paint();
        return;
      }
      try {
        root.localStorage.setItem(storeKey, JSON.stringify(marker));
      } catch (error) {
        /* the mark still works for this page load */
      }
      markLine.textContent = "Marked “" + marker.name + "” at " + Math.round(marker.heading) + "°.";
      paint();
    });

    guideBtn.addEventListener("click", function () {
      ensureAudio();
      guiding = true;
      guideBtn.hidden = true;
      stopBtn.hidden = false;
      paint();
      guideTick();
    });

    stopBtn.addEventListener("click", function () {
      guiding = false;
      guideBtn.hidden = false;
      stopBtn.hidden = true;
      if (guideTimer) clearTimeout(guideTimer);
      paint();
    });

    resetBtn.addEventListener("click", function () {
      walk.reset();
      headingFix.offset = 0;
      headingFix.haveOffset = false;
      headingFix.lastTilt = null;
      guiding = false;
      smoothed = null;
      guideBtn.hidden = false;
      stopBtn.hidden = true;
      try { root.localStorage.removeItem(storeKey); } catch (error) { /* ignore */ }
      markLine.textContent = "Cleared.";
      if (guideTimer) clearTimeout(guideTimer);
      paint();
    });

    if (pulseOnce) pulseOnce.addEventListener("click", pulseRing);

    beepBox.addEventListener("change", function () {
      if (guiding) guideTick();
    });

    practiceHeading.addEventListener("input", function () {
      if (compass) return;
      setHeading(Number(practiceHeading.value), false);
    });

    loadMarker();
    if (!root.isSecureContext) {
      compassLine.textContent = "On the iPhone, open the https address. This page is not secure, so Safari will not share the compass.";
    }
    paint();
  }

  return {
    FACE_DEG: FACE_DEG,
    ACCURACY_MAX: ACCURACY_MAX,
    MARK_ACCURACY_MAX: MARK_ACCURACY_MAX,
    MARK_SPREAD_MAX: MARK_SPREAD_MAX,
    RECENT_MAX: RECENT_MAX,
    TILT_MAX: TILT_MAX,
    wrapDeg: wrapDeg,
    signedDiff: signedDiff,
    normDeg: normDeg,
    poseHeadings: poseHeadings,
    circularMean: circularMean,
    HeadingFix: HeadingFix,
    headingSpread: headingSpread,
    sampleUsable: sampleUsable,
    tiltOk: tiltOk,
    guidance: guidance,
    markRefusal: markRefusal,
    Walk: Walk,
    smoothHeading: smoothHeading,
    headingFromEvent: headingFromEvent,
    sampleFromEvent: sampleFromEvent,
    mount: mount,
  };
});
)RETRACE";
