"""What ReTrace remembers, on disk.

Two layers of visual memory, kept deliberately apart:

  observations     almost every camera picture, including blurry and partly
                   covered ones. Bounded by count, age and size. This is the
                   continuous memory of "what the ring saw".
  landmarks        the subset of observations that are clear enough to match
                   against later. Find and arrival use these only.

Saved items pin the frames they use (references, breadcrumbs, and the context
around the Save press). Pinned frames are never pruned. Everything else is a
ring buffer.

  memory.sqlite
  frames/<id>.jpg
  features/<id>.npz
  imu/imu-<time>.csv
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from app.places import _arrays_to_keypoints, _keypoints_to_arrays

SCHEMA = """
CREATE TABLE IF NOT EXISTS keyframes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unix_ms INTEGER NOT NULL,
    width INTEGER, height INTEGER,
    features INTEGER, sharpness REAL, coverage REAL, brightness REAL,
    contrast REAL NOT NULL DEFAULT 0,
    occlusion REAL NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'observation',
    usable INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT NOT NULL DEFAULT '',
    finger_fraction REAL NOT NULL DEFAULT 0,
    dark_fraction REAL NOT NULL DEFAULT 0,
    frame_class TEXT NOT NULL DEFAULT '',
    bank_keyframe INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL,
    gap_before_s REAL NOT NULL DEFAULT 0,
    imu TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS putdown_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unix_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    peak_accel_g REAL NOT NULL DEFAULT 0,
    stillness_ms REAL NOT NULL DEFAULT 0,
    pin_from_unix_ms INTEGER NOT NULL,
    pin_to_unix_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hold_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unix_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    grasp_unix_ms INTEGER NOT NULL DEFAULT 0,
    hold_ms REAL NOT NULL DEFAULT 0,
    peak_cover REAL NOT NULL DEFAULT 0,
    pin_from_unix_ms INTEGER NOT NULL,
    pin_to_unix_ms INTEGER NOT NULL,
    vlm_raw TEXT NOT NULL DEFAULT '',
    vlm_name TEXT NOT NULL DEFAULT '',
    vlm_description TEXT NOT NULL DEFAULT '',
    vlm_is_none INTEGER NOT NULL DEFAULT 1,
    vlm_error TEXT NOT NULL DEFAULT '',
    item_id INTEGER,
    cover_keyframe INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_unix_ms INTEGER NOT NULL,
    trail_state TEXT NOT NULL,
    trail_note TEXT NOT NULL DEFAULT '',
    anchor_quality TEXT NOT NULL DEFAULT 'missing',
    last_arrived_unix_ms INTEGER
);
CREATE TABLE IF NOT EXISTS item_frames (
    item_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    position INTEGER NOT NULL,
    keyframe_id INTEGER NOT NULL,
    PRIMARY KEY (item_id, kind, position)
);
CREATE INDEX IF NOT EXISTS item_frames_keyframe ON item_frames (keyframe_id);
"""

# Created after _migrate so an older memory.sqlite (no role column yet) can start.
INDEXES = """
CREATE INDEX IF NOT EXISTS keyframes_role ON keyframes (role);
CREATE INDEX IF NOT EXISTS keyframes_unix ON keyframes (unix_ms);
"""

IMU_FIELDS = (
    "host_unix", "board_ms", "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps", "temp_c",
    "ax_raw", "ay_raw", "az_raw", "gx_raw", "gy_raw", "gz_raw",
    "sample_counter", "gap",
)

TRAIL_RECORDING = "recording"
TRAIL_CLOSED = "closed"
CAPTURING = "capturing"

ROLE_OBSERVATION = "observation"
ROLE_LANDMARK = "landmark"

ANCHOR_READY = "ready"
ANCHOR_LIMITED = "limited"
ANCHOR_MISSING = "missing"

KIND_REF = "ref"
KIND_CRUMB = "crumb"
KIND_CONTEXT = "context"


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.frame_dir = self.root / "frames"
        self.feature_dir = self.root / "features"
        self.embed_dir = self.root / "embeddings"
        self.imu_dir = self.root / "imu"
        for folder in (self.frame_dir, self.feature_dir, self.embed_dir, self.imu_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "memory.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.executescript(INDEXES)
            self.db.commit()
        self._imu_file = None
        self._imu_writer = None
        self._imu_started = 0.0
        self._imu_rows = 0

    def _migrate(self) -> None:
        """Older databases from before continuous observations get the new columns."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(keyframes)")}
        for name, declaration in (
            ("contrast", "REAL NOT NULL DEFAULT 0"),
            ("occlusion", "REAL NOT NULL DEFAULT 0"),
            ("role", "TEXT NOT NULL DEFAULT 'observation'"),
            ("usable", "INTEGER NOT NULL DEFAULT 0"),
            ("reject_reason", "TEXT NOT NULL DEFAULT ''"),
            ("finger_fraction", "REAL NOT NULL DEFAULT 0"),
            ("dark_fraction", "REAL NOT NULL DEFAULT 0"),
            ("frame_class", "TEXT NOT NULL DEFAULT ''"),
            ("bank_keyframe", "INTEGER NOT NULL DEFAULT 0"),
            ("pinned", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE keyframes ADD COLUMN {name} {declaration}")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS putdown_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, unix_ms INTEGER NOT NULL, source TEXT NOT NULL,"
            " peak_accel_g REAL NOT NULL DEFAULT 0, stillness_ms REAL NOT NULL DEFAULT 0,"
            " pin_from_unix_ms INTEGER NOT NULL, pin_to_unix_ms INTEGER NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS hold_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, unix_ms INTEGER NOT NULL, source TEXT NOT NULL,"
            " grasp_unix_ms INTEGER NOT NULL DEFAULT 0, hold_ms REAL NOT NULL DEFAULT 0,"
            " peak_cover REAL NOT NULL DEFAULT 0, pin_from_unix_ms INTEGER NOT NULL,"
            " pin_to_unix_ms INTEGER NOT NULL, vlm_raw TEXT NOT NULL DEFAULT '',"
            " vlm_name TEXT NOT NULL DEFAULT '', vlm_description TEXT NOT NULL DEFAULT '',"
            " vlm_is_none INTEGER NOT NULL DEFAULT 1, vlm_error TEXT NOT NULL DEFAULT '',"
            " item_id INTEGER, cover_keyframe INTEGER)"
        )
        item_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(items)")}
        if "pdr_x" not in item_columns:
            self.db.execute("ALTER TABLE items ADD COLUMN pdr_x REAL")
            self.db.execute("ALTER TABLE items ADD COLUMN pdr_y REAL")
        if "body_frame" not in item_columns:
            self.db.execute("ALTER TABLE items ADD COLUMN body_frame TEXT")
        if "anchor_quality" not in item_columns:
            self.db.execute(
                "ALTER TABLE items ADD COLUMN anchor_quality TEXT NOT NULL DEFAULT 'missing'"
            )
            # Frames that used to be "kept" are the closest thing to landmarks.
            self.db.execute("UPDATE keyframes SET role = ? WHERE features >= 40", (ROLE_LANDMARK,))
            for row in self.db.execute("SELECT id FROM items").fetchall():
                self.set_anchor_quality(row["id"], self.compute_anchor_quality(row["id"]))

    def close(self) -> None:
        with self.lock:
            self._close_imu()
            self.db.close()

    # ---- frames ----------------------------------------------------------------------

    def add_frame(
        self,
        result: dict,
        unix_ms: int,
        imu_summary: dict,
        role: str,
        gap_before_s: float = 0.0,
    ) -> int:
        """Write one observation or landmark. role is ROLE_OBSERVATION or ROLE_LANDMARK."""
        arrays = _keypoints_to_arrays(result["keypoints"])
        descriptors = result["descriptors"]
        arrays["descriptors"] = (
            np.zeros((0, 32), dtype=np.uint8) if descriptors is None else np.ascontiguousarray(descriptors)
        )
        occlusion = float(result.get("occlusion", 0.0))
        contrast = float(result.get("contrast", 0.0))
        with self.lock:
            try:
                usable = 1 if result.get("usable") else 0
                reject_reason = str(result.get("reject_reason") or "")
                finger_fraction = float(result.get("finger_fraction") or 0.0)
                dark_fraction = float(result.get("dark_fraction") or 0.0)
                frame_class = str(result.get("frame_class") or "")
                cursor = self.db.execute(
                    "INSERT INTO keyframes (unix_ms, width, height, features, sharpness, coverage, brightness,"
                    " contrast, occlusion, role, usable, reject_reason, finger_fraction, dark_fraction, frame_class,"
                    " bank_keyframe, pinned, bytes, gap_before_s, imu)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
                    (
                        unix_ms,
                        result["width"],
                        result["height"],
                        result["feature_count"],
                        result["blur_score"],
                        result["coverage"],
                        result["brightness"],
                        contrast,
                        occlusion,
                        role,
                        usable,
                        reject_reason,
                        finger_fraction,
                        dark_fraction,
                        frame_class,
                        round(gap_before_s, 2),
                        json.dumps(imu_summary),
                    ),
                )
                keyframe_id = int(cursor.lastrowid)
                jpeg_path = self.frame_dir / f"{keyframe_id}.jpg"
                feature_path = self.feature_dir / f"{keyframe_id}.npz"
                jpeg_path.write_bytes(result["jpeg"])
                np.savez_compressed(feature_path, **arrays)
                size = jpeg_path.stat().st_size + feature_path.stat().st_size
                self.db.execute("UPDATE keyframes SET bytes = ? WHERE id = ?", (size, keyframe_id))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return keyframe_id

    def promote_landmark(self, keyframe_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE keyframes SET role = ? WHERE id = ?", (ROLE_LANDMARK, keyframe_id))

    def keyframe(self, keyframe_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM keyframes WHERE id = ?", (keyframe_id,)).fetchone()
        return None if row is None else self._frame_dict(row)

    def _frame_dict(self, row) -> dict:
        data = dict(row)
        data["imu"] = json.loads(data["imu"]) if data["imu"] else {}
        return data

    def recent_keyframes(self, limit: int, role: str | None = None) -> list[dict]:
        with self.lock:
            if role is None:
                rows = self.db.execute("SELECT * FROM keyframes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM keyframes WHERE role = ? ORDER BY id DESC LIMIT ?", (role, limit)
                ).fetchall()
        return [self._frame_dict(row) for row in rows]

    def keyframes_since(self, unix_ms: int, role: str | None = None) -> list[dict]:
        with self.lock:
            if role is None:
                rows = self.db.execute(
                    "SELECT * FROM keyframes WHERE unix_ms >= ? ORDER BY id", (unix_ms,)
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM keyframes WHERE unix_ms >= ? AND role = ? ORDER BY id",
                    (unix_ms, role),
                ).fetchall()
        return [self._frame_dict(row) for row in rows]

    def jpeg(self, keyframe_id: int) -> bytes | None:
        path = self.frame_dir / f"{int(keyframe_id)}.jpg"
        try:
            return path.read_bytes()
        except OSError:
            return None

    def features(self, keyframe_id: int):
        """Keypoints and descriptors of one frame, or (None, None) when the file is gone."""
        path = self.feature_dir / f"{int(keyframe_id)}.npz"
        try:
            with np.load(path) as data:
                arrays = {name: data[name] for name in data.files}
            descriptors = arrays["descriptors"]
            keypoints = _arrays_to_keypoints(arrays)
        except (OSError, ValueError, KeyError):
            return None, None
        if descriptors.size == 0:
            return keypoints, None
        return keypoints, descriptors

    def prune(
        self,
        max_frames: int,
        max_minutes: float,
        max_mb: float,
        now_unix_ms: int,
        landmark_max_frames: int | None = None,
        bank_max: int | None = None,
    ) -> int:
        """Delete the oldest unpinned frames until every limit holds.

        Item attachments and put-down `pinned` frames are never removed here.
        When bank_max is set, oldest unpinned bank keyframes are dropped first
        until the bank fits, then the usual rolling limits apply.
        """
        with self.lock:
            remove: list[int] = []
            if bank_max is not None:
                bank_rows = self.db.execute(
                    "SELECT id FROM keyframes WHERE bank_keyframe = 1 AND pinned = 0"
                    " AND id NOT IN (SELECT keyframe_id FROM item_frames) ORDER BY id"
                ).fetchall()
                overflow = len(bank_rows) - bank_max
                if overflow > 0:
                    remove.extend(row["id"] for row in bank_rows[:overflow])
                    if remove:
                        self._delete_keyframes(remove)

            rows = self.db.execute(
                "SELECT id, unix_ms, bytes, role FROM keyframes"
                " WHERE id NOT IN (SELECT keyframe_id FROM item_frames) AND pinned = 0 ORDER BY id"
            ).fetchall()
            total = sum(row["bytes"] for row in rows)
            count = len(rows)
            landmarks = sum(1 for row in rows if row["role"] == ROLE_LANDMARK)
            oldest_allowed = now_unix_ms - int(max_minutes * 60_000)
            budget = int(max_mb * 1_000_000)
            landmark_cap = landmark_max_frames if landmark_max_frames is not None else max_frames
            rolling_remove: list[int] = []
            for row in rows:
                if row["id"] in remove:
                    continue
                over_landmarks = landmarks > landmark_cap and row["role"] == ROLE_LANDMARK
                if count > max_frames or total > budget or row["unix_ms"] < oldest_allowed or over_landmarks:
                    rolling_remove.append(row["id"])
                    count -= 1
                    total -= row["bytes"]
                    if row["role"] == ROLE_LANDMARK:
                        landmarks -= 1
                else:
                    break
            self._delete_keyframes(rolling_remove)
            return len(remove) + len(rolling_remove)

    def _delete_keyframes(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.db:
            self.db.executemany("DELETE FROM keyframes WHERE id = ?", [(value,) for value in ids])
        for value in ids:
            (self.frame_dir / f"{value}.jpg").unlink(missing_ok=True)
            (self.feature_dir / f"{value}.npz").unlink(missing_ok=True)
            (self.embed_dir / f"{value}.npy").unlink(missing_ok=True)

    def usage(self) -> dict:
        with self.lock:
            rolling = self.db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b, MIN(unix_ms) AS oldest,"
                " SUM(CASE WHEN role = 'landmark' THEN 1 ELSE 0 END) AS landmarks,"
                " SUM(CASE WHEN usable = 1 THEN 1 ELSE 0 END) AS usable,"
                " SUM(CASE WHEN bank_keyframe = 1 THEN 1 ELSE 0 END) AS bank,"
                " SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) AS pin_flags"
                " FROM keyframes WHERE id NOT IN (SELECT keyframe_id FROM item_frames)"
            ).fetchone()
            pinned = self.db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b,"
                " SUM(CASE WHEN role = 'landmark' THEN 1 ELSE 0 END) AS landmarks"
                " FROM keyframes WHERE id IN (SELECT keyframe_id FROM item_frames)"
            ).fetchone()
            total_usable = self.db.execute("SELECT COUNT(*) AS n FROM keyframes WHERE usable = 1").fetchone()["n"]
            total_bank = self.db.execute("SELECT COUNT(*) AS n FROM keyframes WHERE bank_keyframe = 1").fetchone()["n"]
        imu_bytes = sum(path.stat().st_size for path in self.imu_dir.glob("imu-*.csv"))
        return {
            "rolling_frames": rolling["n"] or 0,
            "rolling_landmarks": rolling["landmarks"] or 0,
            "rolling_usable": rolling["usable"] or 0,
            "rolling_bank": rolling["bank"] or 0,
            "rolling_pinned": rolling["pin_flags"] or 0,
            "rolling_mb": round((rolling["b"] or 0) / 1e6, 2),
            "rolling_oldest_unix_ms": rolling["oldest"],
            "item_frames": pinned["n"] or 0,
            "item_landmarks": pinned["landmarks"] or 0,
            "item_mb": round((pinned["b"] or 0) / 1e6, 2),
            "imu_log_mb": round(imu_bytes / 1e6, 2),
            "usable_frames": total_usable or 0,
            "bank_keyframes": total_bank or 0,
        }

    # ---- DINOv2 bank / put-down pins -------------------------------------------------

    def save_embedding(self, keyframe_id: int, embedding: np.ndarray) -> None:
        path = self.embed_dir / f"{int(keyframe_id)}.npy"
        np.save(path, np.asarray(embedding, dtype=np.float32))

    def load_embedding(self, keyframe_id: int) -> np.ndarray | None:
        path = self.embed_dir / f"{int(keyframe_id)}.npy"
        try:
            return np.load(path)
        except OSError:
            return None

    def mark_bank_keyframe(self, keyframe_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE keyframes SET bank_keyframe = 1 WHERE id = ?", (keyframe_id,))

    def bank_keyframes(self, since_unix_ms: int | None = None) -> list[dict]:
        with self.lock:
            if since_unix_ms is None:
                rows = self.db.execute(
                    "SELECT * FROM keyframes WHERE bank_keyframe = 1 ORDER BY id"
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM keyframes WHERE bank_keyframe = 1 AND unix_ms >= ? ORDER BY id",
                    (since_unix_ms,),
                ).fetchall()
        return [self._frame_dict(row) for row in rows]

    def usable_keyframes_between(self, start_unix_ms: int, end_unix_ms: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM keyframes WHERE usable = 1 AND unix_ms >= ? AND unix_ms <= ? ORDER BY id",
                (start_unix_ms, end_unix_ms),
            ).fetchall()
        return [self._frame_dict(row) for row in rows]

    def keyframes_between(self, start_unix_ms: int, end_unix_ms: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM keyframes WHERE unix_ms >= ? AND unix_ms <= ? ORDER BY id",
                (start_unix_ms, end_unix_ms),
            ).fetchall()
        return [self._frame_dict(row) for row in rows]

    def pin_window(self, start_unix_ms: int, end_unix_ms: int) -> int:
        with self.lock, self.db:
            cursor = self.db.execute(
                "UPDATE keyframes SET pinned = 1 WHERE unix_ms >= ? AND unix_ms <= ?",
                (start_unix_ms, end_unix_ms),
            )
            return int(cursor.rowcount or 0)

    def add_putdown(
        self,
        unix_ms: int,
        source: str,
        peak_accel_g: float,
        stillness_ms: float,
        pin_from_unix_ms: int,
        pin_to_unix_ms: int,
    ) -> int:
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO putdown_events (unix_ms, source, peak_accel_g, stillness_ms, pin_from_unix_ms, pin_to_unix_ms)"
                " VALUES (?,?,?,?,?,?)",
                (unix_ms, source, peak_accel_g, stillness_ms, pin_from_unix_ms, pin_to_unix_ms),
            )
            return int(cursor.lastrowid)

    def putdown_events(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM putdown_events ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def add_hold(
        self,
        unix_ms: int,
        source: str,
        grasp_unix_ms: int,
        hold_ms: float,
        peak_cover: float,
        pin_from_unix_ms: int,
        pin_to_unix_ms: int,
    ) -> int:
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO hold_events (unix_ms, source, grasp_unix_ms, hold_ms, peak_cover,"
                " pin_from_unix_ms, pin_to_unix_ms) VALUES (?,?,?,?,?,?,?)",
                (unix_ms, source, grasp_unix_ms, hold_ms, peak_cover, pin_from_unix_ms, pin_to_unix_ms),
            )
            return int(cursor.lastrowid)

    def update_hold_vlm(
        self,
        hold_id: int,
        *,
        vlm_raw: str,
        vlm_name: str,
        vlm_description: str,
        vlm_is_none: bool,
        vlm_error: str,
        item_id: int | None,
        cover_keyframe: int | None,
    ) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE hold_events SET vlm_raw=?, vlm_name=?, vlm_description=?, vlm_is_none=?,"
                " vlm_error=?, item_id=?, cover_keyframe=? WHERE id=?",
                (
                    vlm_raw,
                    vlm_name,
                    vlm_description,
                    1 if vlm_is_none else 0,
                    vlm_error,
                    item_id,
                    cover_keyframe,
                    hold_id,
                ),
            )

    def hold_events(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM hold_events ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def reject_counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT reject_reason AS reason, COUNT(*) AS n FROM keyframes"
                " WHERE usable = 0 AND reject_reason != '' GROUP BY reject_reason"
            ).fetchall()
        return {row["reason"]: row["n"] for row in rows}

    def class_counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT frame_class AS cls, COUNT(*) AS n FROM keyframes"
                " WHERE frame_class != '' GROUP BY frame_class"
            ).fetchall()
        return {row["cls"]: row["n"] for row in rows}

    def scene_keyframes(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM keyframes WHERE frame_class = 'scene' ORDER BY id"
            ).fetchall()
        return [self._frame_dict(row) for row in rows]


    # ---- items -----------------------------------------------------------------------

    def create_item(self, name: str, unix_ms: int, anchor_quality: str = ANCHOR_MISSING) -> int:
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO items (name, created_unix_ms, trail_state, anchor_quality) VALUES (?,?,?,?)",
                (name, unix_ms, CAPTURING, anchor_quality),
            )
            return int(cursor.lastrowid)

    def set_trail(self, item_id: int, state: str, note: str = "") -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE items SET trail_state = ?, trail_note = ? WHERE id = ?", (state, note, item_id)
            )

    def set_item_position(self, item_id: int, x: float, y: float, frame_id: str | None = None) -> None:
        with self.lock, self.db:
            if frame_id is None:
                self.db.execute(
                    "UPDATE items SET pdr_x = ?, pdr_y = ? WHERE id = ?",
                    (float(x), float(y), item_id),
                )
            else:
                self.db.execute(
                    "UPDATE items SET pdr_x = ?, pdr_y = ?, body_frame = ? WHERE id = ?",
                    (float(x), float(y), frame_id, item_id),
                )

    def set_anchor_quality(self, item_id: int, quality: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE items SET anchor_quality = ? WHERE id = ?", (quality, item_id))

    def set_arrived(self, item_id: int, unix_ms: int) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE items SET last_arrived_unix_ms = ? WHERE id = ?", (unix_ms, item_id))

    def append_frame(self, item_id: int, kind: str, keyframe_id: int) -> int:
        """Add a frame to the end of an item's list. Returns its position, from 1."""
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT 1 FROM item_frames WHERE item_id = ? AND kind = ? AND keyframe_id = ?",
                (item_id, kind, keyframe_id),
            ).fetchone()
            if existing is not None:
                row = self.db.execute(
                    "SELECT position FROM item_frames WHERE item_id = ? AND kind = ? AND keyframe_id = ?",
                    (item_id, kind, keyframe_id),
                ).fetchone()
                return int(row["position"])
            row = self.db.execute(
                "SELECT COALESCE(MAX(position), 0) AS p FROM item_frames WHERE item_id = ? AND kind = ?",
                (item_id, kind),
            ).fetchone()
            position = int(row["p"]) + 1
            self.db.execute(
                "INSERT INTO item_frames (item_id, kind, position, keyframe_id) VALUES (?,?,?,?)",
                (item_id, kind, position, keyframe_id),
            )
            return position

    def item_frame_ids(self, item_id: int, kind: str) -> list[int]:
        with self.lock:
            rows = self.db.execute(
                "SELECT keyframe_id FROM item_frames WHERE item_id = ? AND kind = ? ORDER BY position",
                (item_id, kind),
            ).fetchall()
        return [row["keyframe_id"] for row in rows]

    def compute_anchor_quality(self, item_id: int) -> str:
        refs = self.item_frame_ids(item_id, KIND_REF)
        context = self.item_frame_ids(item_id, KIND_CONTEXT)
        crumbs = self.item_frame_ids(item_id, KIND_CRUMB)
        landmark_refs = 0
        for keyframe_id in refs:
            row = self.keyframe(keyframe_id)
            if row is not None and (row.get("role") == ROLE_LANDMARK or row.get("features", 0) >= 12):
                landmark_refs += 1
        if landmark_refs >= 1:
            return ANCHOR_READY
        if refs or context or crumbs:
            return ANCHOR_LIMITED
        return ANCHOR_MISSING

    def items(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM items ORDER BY id").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["references"] = self.item_frame_ids(item["id"], KIND_REF)
                item["breadcrumbs"] = self.item_frame_ids(item["id"], KIND_CRUMB)
                item["context"] = self.item_frame_ids(item["id"], KIND_CONTEXT)
                if "anchor_quality" not in item or not item["anchor_quality"]:
                    item["anchor_quality"] = ANCHOR_MISSING
                result.append(item)
        return result

    def item(self, item_id: int) -> dict | None:
        for item in self.items():
            if item["id"] == item_id:
                return item
        return None

    def delete_item(self, item_id: int) -> None:
        """Remove an item. Its frames go back to the rolling memory and its limits."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM item_frames WHERE item_id = ?", (item_id,))
            self.db.execute("DELETE FROM items WHERE id = ?", (item_id,))

    # ---- IMU log ---------------------------------------------------------------------

    def _close_imu(self) -> None:
        if self._imu_file is not None:
            self._imu_file.close()
        self._imu_file = None
        self._imu_writer = None

    def log_imu(self, samples: list[dict], file_minutes: float, max_mb: float) -> None:
        if not samples:
            return
        with self.lock:
            now = time.time()
            if self._imu_file is None or now - self._imu_started > file_minutes * 60:
                self._close_imu()
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                path = self.imu_dir / f"imu-{stamp}-{int(now * 1000) % 1000:03d}.csv"
                self._imu_file = path.open("w", encoding="utf-8", newline="")
                self._imu_writer = csv.writer(self._imu_file, lineterminator="\n")
                self._imu_writer.writerow(IMU_FIELDS)
                self._imu_started = now
                self._prune_imu(max_mb)
            for sample in samples:
                self._imu_writer.writerow(
                    [
                        f"{sample.get('host_unix', 0):.4f}",
                        sample.get("sample_ms", ""),
                        *(f"{sample[key]:.5f}" if key in sample else "" for key in IMU_FIELDS[2:9]),
                        *(sample.get(key, "") for key in IMU_FIELDS[9:]),
                    ]
                )
            self._imu_rows += len(samples)
            if self._imu_rows >= 100:
                self._imu_file.flush()
                self._imu_rows = 0

    def _prune_imu(self, max_mb: float) -> None:
        # recordings/pdr_selftest is the kept selftest set. The rolling log must never delete it.
        files = [
            path
            for path in sorted(self.imu_dir.glob("imu-*.csv"))
            if "pdr_selftest" not in path.parts
        ]
        total = sum(path.stat().st_size for path in files)
        budget = max_mb * 1_000_000
        current = None if self._imu_file is None else Path(self._imu_file.name)
        for path in files:
            if total <= budget:
                break
            if current is not None and path == current:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
