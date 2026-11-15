"""Offline place-find heat replay.

    .\\retrace replay-find reports\\weartest_20261114_045012 --mark 2
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ring.config import Config
from ring.embedder import Embedder
from ring.hold_replay import resolve_data_dir, session_bounds_from_report
from ring.place_find import heat_series_for_session
from ring.store import Store

ROOT = Path(__file__).resolve().parents[1]


def _plot_heat(out_path: Path, series: dict, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times = series.get("times_s") or []
    heat = series.get("heat") or []
    fig, ax = plt.subplots(figsize=(12, 4))
    if times:
        ax.plot(times, heat, color="#3fb37f", lw=1.4, label="heat")
    ax.axvline(series.get("grasp_s", 0), color="#d62728", lw=1.4, label="put-down")
    if abs(float(series.get("release_s", 0)) - float(series.get("grasp_s", 0))) > 0.05:
        ax.axvline(series.get("release_s", 0), color="#9467bd", lw=1.2, ls="--", label="release")
    ax.set_ylim(-0.02, 1.05)
    ax.set_xlabel("seconds from session start")
    ax.set_ylabel("heat")
    ax.set_title(title)
    ax.legend(loc="upper right")
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def run_replay_find(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Replay place-find heat for a stored weartest run.")
    parser.add_argument("run_dir", help="reports/weartest_… folder")
    parser.add_argument("--item", type=int, default=0, help="saved item id (uses linked hold window)")
    parser.add_argument("--mark", type=int, default=0, help="marked hold event id (grasp=release=timestamp)")
    parser.add_argument("--data", default="", help="override recordings folder")
    parser.add_argument("--all-marks", action="store_true", help="run every marked event in events.json")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        alt = ROOT / "reports" / args.run_dir
        if alt.is_dir():
            run_dir = alt
        else:
            print(f"Run dir not found: {args.run_dir}", file=sys.stderr)
            return 2

    data = resolve_data_dir(run_dir, args.data or None)
    store = Store(data)
    started, ended = session_bounds_from_report(run_dir, store)
    config = Config(find_backend="place", embedder_enabled=True)
    embedder = Embedder(on_embedding=lambda *_: None, enabled=True)
    try:
        embedder._load()
    except Exception as error:
        print(f"DINOv2 load failed: {type(error).__name__}: {error}", file=sys.stderr)
        store.close()
        return 1

    events_path = run_dir / "events.json"
    events = json.loads(events_path.read_text(encoding="utf-8")) if events_path.is_file() else {}
    marks = [
        e
        for e in events.get("hold_events", [])
        if e.get("source") == "marked" and started <= int(e["unix_ms"]) <= ended
    ]

    jobs: list[dict] = []
    if args.all_marks or (not args.item and not args.mark):
        for mark in marks:
            jobs.append(
                {
                    "label": f"mark_{mark['id']}",
                    "grasp": int(mark["unix_ms"]),
                    "release": int(mark["unix_ms"]),
                    "name": f"mark#{mark['id']}",
                    "place": "",
                }
            )
    elif args.mark:
        mark = next((m for m in marks if int(m["id"]) == int(args.mark)), None)
        if mark is None:
            # fall back to store hold_events
            mark = next((m for m in store.hold_events() if m["id"] == args.mark and m["source"] == "marked"), None)
        if mark is None:
            print(f"Marked event {args.mark} not found in session.", file=sys.stderr)
            store.close()
            return 2
        jobs.append(
            {
                "label": f"mark_{mark['id']}",
                "grasp": int(mark["unix_ms"]),
                "release": int(mark["unix_ms"]),
                "name": f"mark#{mark['id']}",
                "place": "",
            }
        )
    else:
        item = store.item(args.item) if hasattr(store, "item") else None
        if item is None:
            with store.lock:
                row = store.db.execute("SELECT * FROM items WHERE id = ?", (args.item,)).fetchone()
            if row is None:
                print(f"Item {args.item} not found.", file=sys.stderr)
                store.close()
                return 2
            item = dict(row)
        hold = next((h for h in reversed(store.hold_events()) if h.get("item_id") == args.item), None)
        if hold:
            grasp = int(hold.get("grasp_unix_ms") or hold["unix_ms"])
            release = int(hold["unix_ms"])
            place = str(hold.get("vlm_description") or "")
        else:
            grasp = release = int(item.get("created_unix_ms") or started)
            place = str(item.get("trail_note") or "")
        jobs.append(
            {
                "label": f"item_{args.item}",
                "grasp": grasp,
                "release": release,
                "name": item.get("name") or f"item#{args.item}",
                "place": place,
            }
        )

    results = []
    for job in jobs:
        print(f"Replaying {job['label']} ({job['name']})…", flush=True)
        series = heat_series_for_session(
            store=store,
            embedder=embedder,
            config=config,
            grasp_unix_ms=job["grasp"],
            release_unix_ms=job["release"],
            session_start=started,
            session_end=ended,
            name=job["name"],
            place=job["place"],
        )
        if series.get("error"):
            print(f"  error: {series['error']}", flush=True)
            results.append({"label": job["label"], "error": series["error"]})
            continue
        out = run_dir / f"find_heat_{job['label']}.png"
        _plot_heat(
            out,
            series,
            title=f"place heat · {job['name']} · anchors={series.get('anchor_count')} bg={series.get('bg_count')}",
        )
        sheet = run_dir / f"find_sheet_{job['label']}.jpg"
        from ring.hold_replay import write_event_contact_sheet

        sheet_n = write_event_contact_sheet(
            store,
            int(job["release"]),
            sheet,
            before_s=float(config.place_pre_s),
            after_s=float(config.place_post_s),
            step_s=2.0,
        )
        summary = {
            "label": job["label"],
            "name": job["name"],
            "grasp_unix_ms": job["grasp"],
            "release_unix_ms": job["release"],
            "anchor_count": series.get("anchor_count"),
            "bg_count": series.get("bg_count"),
            "heat_max": round(float(max(series["heat"])), 3) if series.get("heat") else None,
            "heat_mean": round(float(np.mean(series["heat"])), 3) if series.get("heat") else None,
            "samples": len(series.get("heat") or []),
            "plot": str(out.name),
            "contact_sheet": sheet.name,
            "contact_frames": sheet_n,
            "ladder": series.get("ladder") or [],
        }
        results.append(summary)
        print(json.dumps(summary, indent=2), flush=True)

    payload = {
        "run_dir": str(run_dir),
        "data_dir": str(data),
        "session": {"started_unix_ms": started, "ended_unix_ms": ended},
        "results": results,
    }
    (run_dir / "replay_find.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    store.close()
    print(f"Wrote {run_dir / 'replay_find.json'}")
    return 0
