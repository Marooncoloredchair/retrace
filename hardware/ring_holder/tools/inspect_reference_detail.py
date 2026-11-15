"""Second-pass inspection of the Seeed STEP: pin holes, corner radii, part stack.

Usage:
    .venv\\Scripts\\python.exe tools\\inspect_reference_detail.py
"""
from collections import defaultdict
from pathlib import Path

import cadquery as cq

HERE = Path(__file__).resolve().parent.parent
STEP = HERE / "reference" / "seeed_xiao_esp32s3_sense" / "Seeed Studio XIAO-ESP32-S3-Sense.step"


def cyl_faces(solid):
    out = []
    for f in solid.Faces():
        if f.geomType() == "CYLINDER":
            bb = f.BoundingBox()
            try:
                r = f.radius()
            except Exception:
                r = None
            out.append((r, bb))
    return out


def main():
    shape = cq.importers.importStep(str(STEP)).val()
    solids = shape.Solids()

    def report(idx, label):
        s = solids[idx]
        bb = s.BoundingBox()
        print(f"\n== solid #{idx} ({label}) ==")
        print(f"   x[{bb.xmin:.3f},{bb.xmax:.3f}] y[{bb.ymin:.3f},{bb.ymax:.3f}] z[{bb.zmin:.3f},{bb.zmax:.3f}]")
        groups = defaultdict(list)
        for r, fb in cyl_faces(s):
            key = None if r is None else round(r, 3)
            groups[key].append(fb)
        for r, fbs in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0])):
            print(f"   cyl r={r}: {len(fbs)} faces")
            centers = sorted(
                {
                    (
                        round((fb.xmin + fb.xmax) / 2, 2),
                        round((fb.zmin + fb.zmax) / 2, 2),
                        round(fb.ymin, 2),
                        round(fb.ymax, 2),
                    )
                    for fb in fbs
                }
            )
            for c in centers[:40]:
                print(f"      bbox-centre x={c[0]:8.2f} z={c[1]:8.2f}  y[{c[2]},{c[3]}]")

    report(37, "main XIAO PCB")
    report(71, "expansion PCB")
    report(101, "camera body")
    report(72, "camera flex/stiffener")
    report(18, "USB-C shell")

    # Planar face heights of the main PCB (thickness check).
    s = solids[37]
    ys = defaultdict(float)
    for f in s.Faces():
        if f.geomType() == "PLANE":
            n = f.normalAt()
            if abs(abs(n.y) - 1) < 1e-6:
                ys[round(f.BoundingBox().ymin, 3)] += f.Area()
    print("\nmain PCB horizontal planar faces (y -> area):")
    for y, a in sorted(ys.items()):
        print(f"   y={y:7.3f}  area={a:9.3f}")

    s = solids[71]
    ys = defaultdict(float)
    for f in s.Faces():
        if f.geomType() == "PLANE":
            n = f.normalAt()
            if abs(abs(n.y) - 1) < 1e-6:
                ys[round(f.BoundingBox().ymin, 3)] += f.Area()
    print("\nexpansion PCB horizontal planar faces (y -> area):")
    for y, a in sorted(ys.items()):
        print(f"   y={y:7.3f}  area={a:9.3f}")

    # Everything that pokes above the main PCB near the USB end, for the
    # camera-rest clearance check (x > 5.8 is beyond the expansion board).
    print("\nsolids with x-centre > 5.8 (USB-end region), sorted by top height:")
    rows = []
    for i, sd in enumerate(solids):
        bb = sd.BoundingBox()
        if (bb.xmin + bb.xmax) / 2 > 5.8:
            rows.append((bb.ymax, i, bb))
    for ymax, i, bb in sorted(rows, reverse=True)[:25]:
        print(
            f"   #{i:3d} top y={ymax:6.3f}  x[{bb.xmin:7.3f},{bb.xmax:7.3f}] "
            f"y[{bb.ymin:6.3f},{bb.ymax:6.3f}] z[{bb.zmin:8.3f},{bb.zmax:8.3f}]"
        )

    print("\nsolids below the main PCB bottom (y < -0.05):")
    for i, sd in enumerate(solids):
        bb = sd.BoundingBox()
        if bb.ymin < -0.26:
            print(f"   #{i:3d} y[{bb.ymin:6.3f},{bb.ymax:6.3f}] x[{bb.xmin:7.3f},{bb.xmax:7.3f}] z[{bb.zmin:8.3f},{bb.zmax:8.3f}]")
    print("   (none listed above means nothing protrudes below y=-0.25)")


if __name__ == "__main__":
    main()
