"""Following a saved breadcrumb trail back to an item.

A trail is an ordered list of pictures. Stage 0 is the saved spot (its
reference frames). Stage k is the k-th breadcrumb kept after leaving it. A
stage number is an order in time. It is not a distance, a coordinate or a
direction, and nothing here turns it into one.

Three layers, each stricter than the one before:

  1. One comparison counts only when a homography has enough agreeing points
     and those points are spread over the picture (ring.vision.compare).
  2. One frame names a stage only when no stage from a different part of the
     trail also fits it. Otherwise the frame is ambiguous and counts for nothing.
  3. A position is confirmed only when several recent frames name the same
     part of the trail and no recent frame names a distant part.

"The same part of the trail" is measured, not assumed: two stages are
neighbours when they are within a few places in order, or when their stored
pictures verify against each other with every stage between them doing the
same (the ring lingered on one scene). A stage outside that run which also
fits the live frame is a second place that looks the same, and that is what
makes a frame ambiguous.

Arrival uses the saved spot's reference frames only, never a breadcrumb.
Progress means a confirmed stage closer to the spot than any confirmed before.
The number of agreeing points is never compared between frames, so a rising
count cannot produce progress.
"""

from __future__ import annotations

import statistics
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ring.vision import compare

UNKNOWN = "UNKNOWN"
MATCHED = "MATCHED"
ARRIVED = "ARRIVED"


@dataclass
class Landmark:
    stage: int
    keyframe_id: int
    keypoints: list
    descriptors: object


class Neighbours:
    """Which stages count as the same part of the trail."""

    def __init__(self, index_window: int, runs: dict[int, tuple[int, int]] | None = None) -> None:
        self.index_window = index_window
        self.runs = runs or {}

    def near(self, a: int, b: int) -> bool:
        if abs(a - b) <= self.index_window:
            return True
        low, high = self.runs.get(a, (a, a))
        if low <= b <= high:
            return True
        low, high = self.runs.get(b, (b, b))
        return low <= a <= high


@dataclass
class Reading:
    """What one live frame says, after layers 1 and 2."""

    at: float
    stage: int | None = None
    inliers: int = 0
    keyframe_id: int | None = None
    ref_hit: bool = False
    ref_inliers: int = 0
    ambiguous: bool = False
    note: str = "no verified match"
    rival_stage: int | None = None
    rival_inliers: int = 0


def _beaten_by(best: int, rival: int, config) -> bool:
    """True when a rival from another part of the trail is too close to the best to ignore."""
    if best < rival + config.inlier_margin:
        return True
    return rival >= config.min_inliers and rival >= config.rival_ratio * best


def read_frame(scores: dict[int, dict], at: float, config, neighbours: Neighbours | None = None) -> Reading:
    """scores maps stage -> {'inliers': n, 'keyframe_id': id} for every stage that was compared."""
    neighbours = neighbours or Neighbours(config.neighbour_stages)
    reading = Reading(at=at)
    if not scores:
        return reading

    def strongest_elsewhere(stage: int) -> tuple[int | None, int]:
        others = {other: item["inliers"] for other, item in scores.items() if not neighbours.near(stage, other)}
        if not others:
            return None, 0
        rival = max(others, key=others.get)
        return rival, others[rival]

    reference = scores.get(0)
    if reference is not None:
        reading.ref_inliers = reference["inliers"]
        _stage, elsewhere = strongest_elsewhere(0)
        reading.ref_hit = reference["inliers"] >= config.min_inliers and not _beaten_by(
            reference["inliers"], elsewhere, config
        )
    best = max(scores, key=lambda stage: (scores[stage]["inliers"], -stage))
    best_inliers = scores[best]["inliers"]
    if best_inliers < config.min_inliers:
        return reading
    rival, rival_inliers = strongest_elsewhere(best)
    reading.rival_stage = rival
    reading.rival_inliers = rival_inliers
    if rival is not None and _beaten_by(best_inliers, rival_inliers, config):
        reading.ambiguous = True
        reading.inliers = best_inliers
        reading.note = f"ambiguous: fits breadcrumb {best} and breadcrumb {rival}, which are different parts of the trail"
        return reading
    reading.stage = best
    reading.inliers = best_inliers
    reading.keyframe_id = scores[best]["keyframe_id"]
    reading.note = "saved spot" if best == 0 else f"breadcrumb {best}"
    return reading


class TrailMatcher:
    """Compares one live frame with every reference and breadcrumb of one item."""

    def __init__(self, landmarks: list[Landmark], config) -> None:
        self.landmarks = [item for item in landmarks if item.descriptors is not None]
        self.config = config
        self.pool = ThreadPoolExecutor(max_workers=max(1, config.match_threads), thread_name_prefix="ring-match")
        self.by_stage: dict[int, list[Landmark]] = {}
        for landmark in self.landmarks:
            self.by_stage.setdefault(landmark.stage, []).append(landmark)
        self.neighbours = Neighbours(config.neighbour_stages, self._runs())

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _same_scene(self, a: int, b: int) -> bool:
        spread = self.config.min_inlier_spread_px
        for first in self.by_stage[a]:
            for second in self.by_stage[b]:
                compared = compare(first.keypoints, first.descriptors, second.keypoints, second.descriptors, spread)
                if compared["inliers"] >= self.config.min_inliers:
                    return True
        return False

    def _runs(self) -> dict[int, tuple[int, int]]:
        """For each stage, the unbroken run of later stages whose stored pictures verify against it."""
        stages = sorted(self.by_stage)
        position = {stage: index for index, stage in enumerate(stages)}
        cap = self.config.same_scene_cap

        def forward(stage: int) -> int:
            reach = stage
            for other in stages[position[stage] + 1 : position[stage] + 1 + cap]:
                if not self._same_scene(stage, other):
                    break
                reach = other
            return reach

        reach = dict(zip(stages, self.pool.map(forward, stages)))
        runs = {}
        for stage in stages:
            low = stage
            for other in reversed(stages[: position[stage]]):
                if reach[other] < stage:
                    break
                low = other
            runs[stage] = (low, reach[stage])
        return runs

    def scores(self, result: dict) -> dict[int, dict]:
        if result.get("descriptors") is None or not self.landmarks:
            return {}
        keypoints, descriptors = result["keypoints"], result["descriptors"]
        spread = self.config.min_inlier_spread_px

        def one(landmark: Landmark) -> dict:
            return compare(keypoints, descriptors, landmark.keypoints, landmark.descriptors, spread)

        scores: dict[int, dict] = {}
        for landmark, compared in zip(self.landmarks, self.pool.map(one, self.landmarks)):
            current = scores.get(landmark.stage)
            if current is None or compared["inliers"] > current["inliers"]:
                scores[landmark.stage] = {"inliers": compared["inliers"], "keyframe_id": landmark.keyframe_id}
        return scores


@dataclass
class TrailTracker:
    """Layer 3. Feed it one Reading per live frame; it returns the events that follow."""

    breadcrumbs: int
    config: object
    neighbours: Neighbours | None = None
    status: str = UNKNOWN
    confirmed_stage: int | None = None
    confirmed_keyframe: int | None = None
    closest_stage: int | None = None
    last_feedback_stage: int | None = None
    last_confirmed_at: float | None = None
    joined: bool = False
    arrived_at: float | None = None
    visited: set = field(default_factory=set)
    window: deque = field(default_factory=deque)
    last: Reading | None = None

    def __post_init__(self) -> None:
        if self.neighbours is None:
            self.neighbours = Neighbours(self.config.neighbour_stages)

    def _trim(self, now: float) -> None:
        while self.window and (
            len(self.window) > self.config.window_frames or now - self.window[0].at > self.config.window_seconds
        ):
            self.window.popleft()

    def _far(self, a: int, b: int) -> bool:
        return abs(a - b) > self.config.conflict_stages and not self.neighbours.near(a, b)

    def tick(self, now: float) -> list[dict]:
        """Call when time passes without a frame, so a stale match does not stay on screen."""
        if self.status == MATCHED and self.last_confirmed_at is not None:
            if now - self.last_confirmed_at > self.config.window_seconds:
                self.status = UNKNOWN
                self.window.clear()
                return [{"kind": "lost", "stage": self.confirmed_stage}]
        return []

    def update(self, reading: Reading) -> list[dict]:
        if self.status == ARRIVED:
            return []
        config = self.config
        now = reading.at
        self.last = reading
        self.window.append(reading)
        self._trim(now)
        named = [item for item in self.window if item.stage is not None]
        ambiguous = sum(1 for item in self.window if item.ambiguous)

        # Arrival: the saved spot's own reference frames, several times, nothing pointing far away.
        if reading.ref_hit:
            hits = sum(1 for item in self.window if item.ref_hit)
            far = [item for item in named if self._far(0, item.stage)]
            if hits >= config.arrival_required_frames and not far and ambiguous < hits:
                self.status = ARRIVED
                self.arrived_at = now
                self.confirmed_stage = 0
                self.closest_stage = 0
                self.last_confirmed_at = now
                self.visited.add(0)
                return [{"kind": "arrived", "stage": 0}]

        events: list[dict] = []
        confirmed = False
        if reading.stage is not None:
            supporters = [item for item in named if self.neighbours.near(reading.stage, item.stage)]
            conflicts = [item for item in named if self._far(reading.stage, item.stage)]
            required = config.required_frames
            if (
                self.confirmed_stage is not None
                and self.last_confirmed_at is not None
                and now - self.last_confirmed_at <= config.jump_window_s
                and abs(reading.stage - self.confirmed_stage) > config.jump_stages
                and not self.neighbours.near(reading.stage, self.confirmed_stage)
            ):
                required = config.jump_required_frames
            if len(supporters) >= required and not conflicts and ambiguous < len(supporters):
                confirmed = True
                # The middle of the newest supporting frames: steadier than one frame, without lagging the window.
                stage = int(statistics.median_low(sorted(item.stage for item in supporters[-required:])))
                was_unknown = self.status == UNKNOWN
                self.status = MATCHED
                self.confirmed_stage = stage
                self.confirmed_keyframe = reading.keyframe_id
                self.last_confirmed_at = now
                self.visited.add(stage)
                if not self.joined:
                    self.joined = True
                    self.closest_stage = stage
                    self.last_feedback_stage = stage
                    events.append({"kind": "joined", "stage": stage})
                else:
                    if was_unknown:
                        events.append({"kind": "recovered", "stage": stage})
                    if stage < self.closest_stage:
                        self.closest_stage = stage
                        if stage <= self.last_feedback_stage - config.progress_step:
                            self.last_feedback_stage = stage
                            events.append({"kind": "progress", "stage": stage})
        if not confirmed:
            events.extend(self.tick(now))
        return events
