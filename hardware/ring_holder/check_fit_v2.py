"""Fit checks for the V2 holders against reference geometry.

    .venv\\Scripts\\python.exe check_fit_v2.py

These checks show that the holder solid does not overlap Seeed's CAD model of
the board or the nominal proxy parts. They do NOT prove the print fits the
physical hardware: jumpers, plug, camera position, finger and heat sink are
assumed sizes, and the real board has not been measured.
"""
from __future__ import annotations

import sys

import cadquery as cq

import reference_models as ref
import reference_models_v2 as ref2
from check_fit import LABELS, TOL_VOL, common_volume, min_distance
from retrace_ring_v2 import OUT, VARIANTS, LayoutV2, build_holder_v2


def check(name: str, p) -> int:
    L = LayoutV2(p)
    holder = build_holder_v2(p).val()
    xiao = ref.load_xiao(L)
    uses_duponts = p.under_pcb_clearance >= 18.0
    failures = 0
    print(f"\n######## {name}  (under-board space {p.under_pcb_clearance} mm) ########")

    print("== 1. Seated: overlap volume with the holder (must be 0) ==")
    seated = {
        "XIAO Sense board (Seeed STEP, camera + SD card removed)": xiao["board"],
        "microSD card (Seeed STEP)": xiao["sd_card"],
        "OV3660 proxy on the camera rest": ref.ov3660(L, xiao["camera_cad"]),
        "header strips + full-length pins (nominal proxy)": ref.header_strips(L),
        "USB-C plug (assumed 12.4 x 6.8 overmold)": ref.usb_plug(L),
        "gate pin (1.75 filament)": ref.gate_pin(L),
        "retaining stub (1.75 filament)": ref2.retaining_stub(L),
        f"finger (ASSUMED {p.finger_d:.0f} mm cylinder)": ref2.finger(L),
    }
    if uses_duponts:
        seated["Dupont housings on all 14 pins (nominal proxy)"] = ref.duponts(L, ref.ALL_PINS)
    if p.imu_shelf:
        seated["GY-521 on the shelf with jumpers (estimated proxy)"] = ref.imu_board(L)
    for label, shape in seated.items():
        v = common_volume(holder, shape)
        ok = v <= TOL_VOL
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {v:9.4f} mm3  {label}")

    # The retaining stub must also clear the board it holds down.
    v = common_volume(xiao["board"], ref2.retaining_stub(L))
    ok = v <= TOL_VOL
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {v:9.4f} mm3  retaining stub vs board (Seeed STEP)")

    print("== 2. Slide-in path (board enters from the fingertip end, pins removed) ==")
    moving_parts = [xiao["board"], xiao["sd_card"], ref.header_strips(L)]
    if uses_duponts:
        moving_parts.append(ref.duponts(L, ref.ALL_PINS))
    moving = cq.Compound.makeCompound(moving_parts)
    travel = L.x_pcb_max - L.x_wall_min + 2.0
    step = 0.5
    n = int(travel / step) + 1
    worst, bad = 0.0, []
    for k in range(n + 1):
        v = common_volume(holder, moving.translate(cq.Vector(-k * step, 0, 0)))
        worst = max(worst, v)
        if not v <= TOL_VOL:
            bad.append((k * step, v))
    failures += bool(bad)
    print(f"  {'ok  ' if not bad else 'FAIL'} {n + 1} positions over {n * step:.1f} mm in {step} mm steps, worst overlap {worst:.4f} mm3")
    for dx, v in bad[:8]:
        print(f"       overlap {v:.4f} mm3 with the board {dx:.1f} mm short of seated")

    print("== 3. Clearances, seated (mm) ==")
    rows = [(min_distance(holder, xiao["by_index"][i]), label + " [Seeed STEP]") for i, label in LABELS.items()]
    rows.append((min_distance(holder, xiao["sd_card"]), "microSD card [Seeed STEP]"))
    rows.append((min_distance(holder, ref.header_strips(L)), "header strips + pins [nominal proxy]"))
    rows.append((min_distance(holder, ref.usb_plug(L)), "USB-C plug [assumed proxy]"))
    stub = ref2.retaining_stub(L)
    rows.append((min_distance(stub, xiao["by_index"][37]), "retaining stub to main PCB top (the surface it holds)"))
    rows.append((min_distance(stub, xiao["by_index"][71]), "retaining stub to expansion PCB underside"))
    rows.append((min_distance(stub, xiao["by_index"][35]), "retaining stub to U.FL antenna connector"))
    rows.append((min_distance(stub, xiao["by_index"][49]), "retaining stub to board-to-board connector"))
    rows.append((min_distance(stub, xiao["by_index"][98]), "retaining stub to camera FPC connector"))
    rows.append((min_distance(stub, ref.header_strips(L)), "retaining stub to nearest header pin tail [nominal proxy]"))
    rows.append((min_distance(holder, ref2.finger(L)), "finger to holder (0 = touching the V saddle, as intended)"))
    if uses_duponts:
        rows.append((min_distance(holder, ref.duponts(L, ref.ALL_PINS)), "Dupont housings [nominal proxy]"))
    for d, label in sorted(rows):
        print(f"  {d:6.2f}  {label}")

    print("== 4. Exported files ==")
    re_step = cq.importers.importStep(str(OUT / f"{name}.step")).val()
    ns = len(re_step.Solids())
    dv = abs(re_step.Volume() - holder.Volume())
    ok = ns == 1 and re_step.isValid() and dv < 0.5
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} STEP re-import: {ns} solid, valid={re_step.isValid()}, volume diff {dv:.4f} mm3")
    import trimesh

    mesh = trimesh.load_mesh(str(OUT / f"{name}.stl"))
    ok = bool(mesh.is_watertight and mesh.is_winding_consistent)
    failures += not ok
    e = mesh.extents
    print(f"  {'ok  ' if ok else 'FAIL'} STL: {len(mesh.faces)} triangles, watertight={mesh.is_watertight}, extents {e[0]:.2f} x {e[1]:.2f} x {e[2]:.2f}")
    return failures


def main() -> int:
    total = 0
    for name, (p, _desc) in VARIANTS.items():
        total += check(name, p)
    print(f"\n{'ALL CHECKS PASSED' if total == 0 else f'{total} CHECK(S) FAILED'}")
    print("Reminder: passing these checks is not a fit guarantee. Sizes tagged ASSUMED / ESTIMATED are unmeasured.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
