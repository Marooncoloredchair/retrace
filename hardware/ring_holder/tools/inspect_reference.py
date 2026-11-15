"""Inspect the Seeed XIAO ESP32S3 Sense manufacturer STEP file.

Prints the overall bounding box and the bounding box of every solid so the
holder can be designed around real manufacturer geometry instead of guesses.

Usage:
    .venv\\Scripts\\python.exe tools\\inspect_reference.py
"""
from pathlib import Path

import cadquery as cq

HERE = Path(__file__).resolve().parent.parent
STEP = HERE / "reference" / "seeed_xiao_esp32s3_sense" / "Seeed Studio XIAO-ESP32-S3-Sense.step"


def fmt(bb):
    return (
        f"x[{bb.xmin:8.3f},{bb.xmax:8.3f}] "
        f"y[{bb.ymin:8.3f},{bb.ymax:8.3f}] "
        f"z[{bb.zmin:8.3f},{bb.zmax:8.3f}]  "
        f"size {bb.xlen:7.3f} x {bb.ylen:7.3f} x {bb.zlen:7.3f}"
    )


def main():
    model = cq.importers.importStep(str(STEP))
    shape = model.val()
    print("file:", STEP.name)
    print("overall:", fmt(shape.BoundingBox()))
    solids = shape.Solids()
    print("solid count:", len(solids))
    rows = []
    for i, s in enumerate(solids):
        bb = s.BoundingBox()
        rows.append((s.Volume(), i, bb))
    rows.sort(reverse=True)
    print("\nlargest 60 solids by volume:")
    for vol, i, bb in rows[:60]:
        print(f"#{i:4d} vol {vol:9.3f}  {fmt(bb)}")


if __name__ == "__main__":
    main()
