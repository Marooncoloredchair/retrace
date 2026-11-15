const assert = require("assert");
const pocket = require("../ring/web/pocket.js");

function close(a, b) {
  assert.ok(Math.abs(pocket.wrapDeg(a - b)) < 1e-6, a + " vs " + b);
}

close(pocket.wrapDeg(0), 0);
close(pocket.wrapDeg(190), -170);
close(pocket.wrapDeg(-190), 170);
close(pocket.circularMean([0, 90]), 45);
close(pocket.circularMean([350, 10]), 0);

const straight = pocket.guidance({ heading: 10, marker: { heading: 0 } });
assert.strictEqual(straight.word, "Straight");
assert.strictEqual(straight.facing, true);

const left = pocket.guidance({ heading: 40, marker: { heading: 0 } });
assert.strictEqual(left.word, "Turn left");
assert.strictEqual(left.facing, false);
assert.ok(Math.abs(left.errorDeg + 40) < 1e-6);

const right = pocket.guidance({ heading: 320, marker: { heading: 0 } });
assert.strictEqual(right.word, "Turn right");
assert.ok(Math.abs(right.errorDeg - 40) < 1e-6);

const away = pocket.guidance({ heading: 0, marker: { heading: 180 } });
assert.strictEqual(away.facing, false);
assert.ok(Math.abs(Math.abs(away.errorDeg) - 180) < 1e-6);

assert.strictEqual(pocket.guidance({ heading: 0, marker: null }).word, "No mark");
assert.strictEqual(pocket.guidance({ heading: 0, marker: { heading: 0 }, compassBad: true }).word, "Unsteady");
assert.strictEqual(pocket.guidance({ heading: 0, marker: { heading: 0 }, compassBad: true }).facing, false);
assert.strictEqual(pocket.guidance({ heading: 0, marker: { heading: 0 }, tiltBad: true }).word, "Lay flat");
assert.strictEqual(pocket.guidance({ heading: 0, marker: { heading: 0 }, upsideDown: true }).word, "Turn around");

const practice = new pocket.Walk();
practice.setHeading(184);
const marked = practice.mark("corner");
assert.strictEqual(marked.heading, 184);
assert.strictEqual(practice.guide().word, "Straight");
practice.setHeading(224);
assert.strictEqual(practice.guide().word, "Turn left");

const live = new pocket.Walk();
live.noteSample({ heading: 10, accuracy: 8, beta: 5, gamma: 2 });
live.noteSample({ heading: 12, accuracy: 8, beta: 5, gamma: 2 });
live.noteSample({ heading: 14, accuracy: 8, beta: 5, gamma: 2 });
const mean = live.mark("door");
assert.ok(Math.abs(pocket.wrapDeg(mean.heading - 12)) < 0.2);
live.setHeading(12);
assert.strictEqual(live.guide().word, "Straight");

const shaky = new pocket.Walk();
shaky.noteSample({ heading: 10, accuracy: -1, beta: 0, gamma: 0 });
shaky.noteSample({ heading: 10, accuracy: -1, beta: 0, gamma: 0 });
shaky.noteSample({ heading: 10, accuracy: -1, beta: 0, gamma: 0 });
assert.strictEqual(shaky.mark("nope"), null);
assert.strictEqual(shaky.compassBad, true);

const tilted = new pocket.Walk();
tilted.noteSample({ heading: 20, accuracy: 5, beta: 5, gamma: 0 });
tilted.noteSample({ heading: 20, accuracy: 5, beta: 5, gamma: 0 });
tilted.noteSample({ heading: 20, accuracy: 5, beta: 5, gamma: 0 });
const held = tilted.mark("wall");
assert.strictEqual(held.heading, 20);
tilted.noteSample({ heading: 20, accuracy: 5, beta: 80, gamma: 0 });
assert.strictEqual(tilted.tiltBad, false);
assert.strictEqual(tilted.guide().word, "Straight");

const sample = pocket.sampleFromEvent({
  webkitCompassHeading: 20,
  webkitCompassAccuracy: 12,
  beta: 4,
  gamma: -3,
});
assert.strictEqual(sample.heading, 20);
assert.strictEqual(sample.accuracy, 12);
assert.strictEqual(sample.beta, 4);
assert.strictEqual(pocket.sampleUsable(sample), true);
assert.strictEqual(pocket.sampleUsable({ heading: 1, accuracy: -1 }), false);
assert.strictEqual(pocket.sampleUsable({ heading: 1, accuracy: 55 }), false);
assert.strictEqual(pocket.sampleUsable({ heading: 1, accuracy: null }), true);
assert.strictEqual(pocket.tiltOk({ beta: 10, gamma: 10 }), true);
assert.strictEqual(pocket.tiltOk({ beta: 70, gamma: 0 }), true);
assert.strictEqual(pocket.tiltOk({ beta: 0, gamma: 70 }), true);
assert.strictEqual(pocket.tiltOk({ beta: 170, gamma: 0 }), false);

assert.ok(Math.abs(pocket.smoothHeading(0, 40, 0)) > Math.abs(pocket.smoothHeading(0, 40, 40)));

assert.strictEqual(pocket.headingFromEvent({ webkitCompassHeading: 184, alpha: 10 }), 184);
assert.strictEqual(pocket.headingFromEvent({ absolute: true, alpha: 90 }), 270);
assert.strictEqual(pocket.headingFromEvent({ alpha: 90 }), null);
close(pocket.signedDiff(10, 350), 20);
close(pocket.signedDiff(350, 10), -20);
close(pocket.signedDiff(323, 143), -180);

function assertContinuous(values, maxStep, label) {
  for (let i = 1; i < values.length; i += 1) {
    const step = Math.abs(pocket.signedDiff(values[i], values[i - 1]));
    assert.ok(step <= maxStep, label + " jump " + step + " from " + values[i - 1] + " to " + values[i]);
  }
}

function poseEvent(forwardDeg, beta, alphaBias) {
  const bias = alphaBias || 0;
  const alpha = pocket.normDeg(360 - forwardDeg + bias);
  const pose = pocket.poseHeadings(alpha, beta, 0);
  const topOk = pose.top && pose.top.horizontal >= 0.4;
  // iOS webkitCompassHeading is the real compass of the top edge. Alpha's zero is not north.
  const trueTop = pose.top ? pocket.normDeg(pose.top.deg + bias) : null;
  return {
    alpha: alpha,
    beta: beta,
    gamma: 0,
    absolute: true,
    webkitCompassAccuracy: 8,
    webkitCompassHeading: topOk ? trueTop : pocket.normDeg(forwardDeg + 180),
  };
}

const throughNorth = new pocket.HeadingFix();
const northSeq = [];
for (let heading = 350; heading !== 12; heading = (heading + 2) % 360) {
  northSeq.push(throughNorth.read(poseEvent(heading, 0)).tiltSafe);
  if (heading === 10) break;
}
assertContinuous(northSeq, 5, "through north");
close(pocket.signedDiff(northSeq[0], 350), 0);
close(pocket.signedDiff(northSeq[northSeq.length - 1], 10), 0);

const aboutFace = new pocket.HeadingFix();
const turnSeq = [];
for (let step = 0; step <= 180; step += 10) {
  turnSeq.push(aboutFace.read(poseEvent((323 + step) % 360, 30)).tiltSafe);
}
assertContinuous(turnSeq, 15, "turn 180");
close(pocket.signedDiff(turnSeq[0], 323), 0);
assert.ok(Math.abs(pocket.signedDiff(turnSeq[turnSeq.length - 1], 143)) < 1e-6);

const tiltUp = new pocket.HeadingFix();
const tiltSeq = [];
for (let beta = 0; beta <= 130; beta += 5) {
  tiltSeq.push(tiltUp.read(poseEvent(323, beta, 17)).tiltSafe);
}
assertContinuous(tiltSeq, 12, "tilt flat to past upright");
tiltSeq.forEach(function (heading) {
  assert.ok(Math.abs(pocket.signedDiff(heading, 323)) < 8, "tilt drifted to " + heading);
});
const pastVertical = new pocket.HeadingFix();
pastVertical.read(poseEvent(323, 0, 17));
const upright = pastVertical.read(poseEvent(323, 110, 17));
assert.ok(Math.abs(pocket.signedDiff(upright.tiltSafe, 323)) < 8);
assert.ok(Math.abs(Math.abs(pocket.signedDiff(upright.raw, upright.tiltSafe)) - 180) < 25);

assert.ok(Math.abs(pocket.headingSpread([10, 12, 14]) - 2) < 1e-6);
assert.ok(Math.abs(pocket.headingSpread([350, 10]) - 10) < 1e-6);
const spreadEdge = [0, pocket.MARK_SPREAD_MAX, pocket.MARK_SPREAD_MAX * 2];
assert.ok(Math.abs(pocket.headingSpread(spreadEdge) - pocket.MARK_SPREAD_MAX) < 1e-6);
assert.ok(pocket.headingSpread([0, 70, 140]) > pocket.MARK_SPREAD_MAX);

function fill(walk, heading, accuracy, count) {
  for (let i = 0; i < count; i += 1) {
    walk.noteSample({ heading: heading, accuracy: accuracy, beta: 0, gamma: 0 });
  }
}

const atAccuracy = new pocket.Walk();
fill(atAccuracy, 30, pocket.MARK_ACCURACY_MAX, 4);
assert.strictEqual(atAccuracy.mark("tight").heading, 30);
assert.strictEqual(atAccuracy.settling, false);

const poorAccuracy = new pocket.Walk();
fill(poorAccuracy, 30, pocket.MARK_ACCURACY_MAX + 1, 4);
assert.strictEqual(pocket.sampleUsable({ heading: 30, accuracy: pocket.MARK_ACCURACY_MAX + 1 }), true);
assert.strictEqual(poorAccuracy.mark("early"), null);
assert.strictEqual(poorAccuracy.settling, true);
assert.strictEqual(poorAccuracy.marker, null);
assert.ok(pocket.markRefusal(poorAccuracy).indexOf("still settling") !== -1);

const swinging = new pocket.Walk();
[0, 70, 140].forEach(function (heading) {
  swinging.noteSample({ heading: heading, accuracy: 5, beta: 0, gamma: 0 });
});
assert.strictEqual(swinging.mark("corner"), null);
assert.strictEqual(swinging.settling, true);
assert.ok(pocket.markRefusal(swinging).indexOf("still settling") !== -1);

const edge = new pocket.Walk();
spreadEdge.forEach(function (heading) {
  edge.noteSample({ heading: heading, accuracy: 5, beta: 0, gamma: 0 });
});
const edgeMark = edge.mark("edge");
assert.ok(edgeMark);
assert.ok(Math.abs(pocket.wrapDeg(edgeMark.heading - pocket.MARK_SPREAD_MAX)) < 1e-6);

const overSpread = new pocket.Walk();
const over = pocket.MARK_SPREAD_MAX + 1;
[0, over, over * 2].forEach(function (heading) {
  overSpread.noteSample({ heading: heading, accuracy: 5, beta: 0, gamma: 0 });
});
assert.ok(pocket.headingSpread(overSpread.recent) > pocket.MARK_SPREAD_MAX);
assert.strictEqual(overSpread.mark("over"), null);
assert.strictEqual(overSpread.settling, true);

const later = new pocket.Walk();
[10, 80, 150].forEach(function (heading) {
  later.noteSample({ heading: heading, accuracy: 8, beta: 0, gamma: 0 });
});
assert.strictEqual(later.mark("early"), null);
fill(later, 200, 5, pocket.RECENT_MAX);
const settled = later.mark("now");
assert.ok(Math.abs(pocket.wrapDeg(settled.heading - 200)) < 1e-6);
assert.strictEqual(later.settling, false);
later.setHeading(240);
assert.strictEqual(later.guide().word, "Turn left");

const html = require("fs").readFileSync(require("path").join(__dirname, "../ring/web/pocket.html"), "utf8");
assert.ok(html.includes("±" + pocket.MARK_ACCURACY_MAX + "°"));
assert.ok(html.includes(pocket.MARK_SPREAD_MAX + "° of their average"));
assert.ok(html.includes('id="rose-phone"'));
assert.ok(html.includes('id="rose-mark"'));
assert.ok(html.includes('id="raw-heading"'));
assert.ok(html.includes('id="diff"'));
assert.ok(html.includes("back of the phone"));

console.log("pocket guide ok");
