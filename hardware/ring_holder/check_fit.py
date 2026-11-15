"""Fit checks for the holder against the reference geometry.

    .venv\\Scripts\\python.exe check_fit.py

What this does and does not prove:
  * It proves the holder solid does not overlap Seeed's CAD model of the board,
    or the nominal proxy solids, in the seated position and along the slide-in path.
  * It does NOT prove the print fits the physical hardware. The proxies use
    assumed sizes; the real board has not been measured.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cadquery as cq
from OCP.BRepExtrema import BRepExtrema_DistShapeShape

import reference_models as ref
from retrace_ring_v1 import NAME, OUT, Layout, Params, build_holder

TOL_VOL = 1e-3  # mm^3

LABELS = {
    37: "main PCB",
    71: "expansion PCB",
    18: "USB-C shell",
    25: "button (one side)",
    30: "button (other side)",
    35: "U.FL antenna connector",
    24: "ESP32-S3 shield can",
    70: "microSD socket",
    98: "camera FPC connector",
    49: "board-to-board connector",
}


def common_volume(a: cq.Shape, b: cq.Shape) -> float:
    try:
        return a.intersect(b).Volume()
    except Exception:
        return float("nan")


def min_distance(a: cq.Shape, b: cq.Shape) -> float:
    d = BRepExtrema_DistShapeShape(a.wrapped, b.wrapped)
    d.Perform()
    return d.Value()


def main() -> int:
    p = Params()
    L = Layout(p)
    holder = build_holder(p).val()
    xiao = ref.load_xiao(L)
    failures = 0

    print("== 1. Seated position: overlap volume with the holder (must be 0) ==")
    seated = {
        "XIAO Sense board (Seeed STEP, camera + SD card removed)": xiao["board"],
        "microSD card (Seeed STEP)": xiao["sd_card"],
        "OV3660 proxy on the camera rest": ref.ov3660(L, xiao["camera_cad"]),
        "header strips + pins (nominal proxy)": ref.header_strips(L),
        "Dupont housings on all 14 pins (nominal proxy)": ref.duponts(L, ref.ALL_PINS),
        "USB-C plug (assumed 12.4 x 6.8 overmold)": ref.usb_plug(L),
        "GY-521 on the shelf (estimated proxy)": ref.imu_board(L),
        "gate pin (1.75 filament)": ref.gate_pin(L),
    }
    for name, shape in seated.items():
        v = common_volume(holder, shape)
        ok = v <= TOL_VOL
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {v:9.4f} mm3  {name}")

    print("\n== 2. Slide-in path: board enters from the fingertip end, moving toward the wrist ==")
    moving = cq.Compound.makeCompound(
        [xiao["board"], xiao["sd_card"], ref.header_strips(L), ref.duponts(L, ref.ALL_PINS)]
    )
    travel = L.x_pcb_max - L.x_wall_min + 2.0
    step = 0.5  # finer than the thinnest holder feature along the slide direction (1.1 mm rail)
    n = int(travel / step) + 1
    worst, bad = 0.0, []
    for k in range(n + 1):
        dx = k * step
        v = common_volume(holder, moving.translate(cq.Vector(-dx, 0, 0)))
        worst = max(worst, v)
        if not v <= TOL_VOL:
            bad.append((dx, v))
    failures += bool(bad)
    print(f"  {'ok  ' if not bad else 'FAIL'} {n + 1} positions over {n * step:.1f} mm in {step} mm steps, worst overlap {worst:.4f} mm3")
    for dx, v in bad[:10]:
        print(f"       overlap {v:.4f} mm3 with the board {dx:.1f} mm short of seated")

    print("\n== 3. Clearances in the seated position (mm) ==")
    rows = []
    for idx, label in LABELS.items():
        rows.append((min_distance(holder, xiao["by_index"][idx]), label + " [Seeed STEP]"))
    rows.append((min_distance(holder, xiao["sd_card"]), "microSD card [Seeed STEP]"))
    rows.append((min_distance(holder, ref.duponts(L, ref.ALL_PINS)), "Dupont housings [nominal proxy]"))
    rows.append((min_distance(holder, ref.header_strips(L)), "header strips + pins [nominal proxy]"))
    rows.append((min_distance(holder, ref.usb_plug(L)), "USB-C plug [assumed proxy]"))
    for d, label in sorted(rows):
        note = "  (rests on the holder)" if d < 1e-6 else ""
        print(f"  {d:6.2f}  {label}{note}")

    print("\n== 4. Exported files ==")
    step_path = OUT / f"{NAME}.step"
    stl_path = OUT / f"{NAME}.stl"
    re_step = cq.importers.importStep(str(step_path)).val()
    n = len(re_step.Solids())
    dv = abs(re_step.Volume() - holder.Volume())
    ok = n == 1 and re_step.isValid() and dv < 0.5
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} STEP re-import: {n} solid, valid={re_step.isValid()}, volume diff {dv:.4f} mm3")
    try:
        import trimesh

        mesh = trimesh.load_mesh(str(stl_path))
        ok = bool(mesh.is_watertight and mesh.is_winding_consistent)
        failures += not ok
        ext = mesh.extents
        print(
            f"  {'ok  ' if ok else 'FAIL'} STL: {len(mesh.faces)} triangles, watertight={mesh.is_watertight}, "
            f"volume {mesh.volume:.1f} mm3 (CAD {holder.Volume():.1f}), extents {ext[0]:.2f} x {ext[1]:.2f} x {ext[2]:.2f}"
        )
    except ImportError:
        print("  note: trimesh not installed, STL watertight check skipped")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
