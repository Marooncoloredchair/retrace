"""Render PNG views of the real CAD geometry (tessellated B-rep, offscreen VTK).

    .venv\\Scripts\\python.exe render_views.py

Writes PNGs to ./out/views. The holder is the printed part; everything else in
the "assembled" views is reference geometry (Seeed STEP + labelled proxies).
"""
from __future__ import annotations

from pathlib import Path

import cadquery as cq
import vtk

import reference_models as ref
from retrace_ring_v1 import NAME, OUT, Layout, Params, build_holder

VIEWS = OUT / "views"
SIZE = (1800, 1350)

HOLDER = (0.96, 0.55, 0.15)
BOARD = (0.20, 0.45, 0.30)
CAMERA = (0.12, 0.12, 0.14)
JUMPER = (0.75, 0.15, 0.15)
HEADER = (0.15, 0.15, 0.15)
PLUG = (0.82, 0.82, 0.85)
IMU = (0.15, 0.30, 0.75)
SKIN = (0.93, 0.76, 0.65)
STRAP = (0.10, 0.10, 0.10)
PIN = (0.95, 0.95, 0.30)


def actors_for(shape: cq.Shape, color, opacity=1.0, edges=True, edge_color=(0.05, 0.05, 0.05)):
    pd = shape.toVtkPolyData(0.02, 0.1)
    faces = vtk.vtkPolyData()
    faces.SetPoints(pd.GetPoints())
    faces.SetPolys(pd.GetPolys())
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(faces)
    normals.SetFeatureAngle(35)
    normals.SplittingOn()
    fm = vtk.vtkPolyDataMapper()
    fm.SetInputConnection(normals.GetOutputPort())
    fa = vtk.vtkActor()
    fa.SetMapper(fm)
    prop = fa.GetProperty()
    prop.SetColor(*color)
    prop.SetOpacity(opacity)
    prop.SetAmbient(0.30)
    prop.SetDiffuse(0.70)
    prop.SetSpecular(0.08)
    out = [fa]
    if edges:
        lines = vtk.vtkPolyData()
        lines.SetPoints(pd.GetPoints())
        lines.SetLines(pd.GetLines())
        lm = vtk.vtkPolyDataMapper()
        lm.SetInputData(lines)
        lm.SetResolveCoincidentTopologyToPolygonOffset()
        la = vtk.vtkActor()
        la.SetMapper(lm)
        la.GetProperty().SetColor(*edge_color)
        la.GetProperty().SetLineWidth(1.6)
        la.GetProperty().SetOpacity(min(1.0, opacity + 0.35))
        out.append(la)
    return out


def text(msg, x, y, size=26, color=(0.1, 0.1, 0.1), bold=False):
    t = vtk.vtkTextActor()
    t.SetInput(msg)
    t.GetTextProperty().SetFontSize(size)
    t.GetTextProperty().SetColor(*color)
    t.GetTextProperty().SetBold(bold)
    t.GetTextProperty().SetFontFamilyToArial()
    t.SetDisplayPosition(x, y)
    return t


def render(path: Path, items, direction, up, title, notes=(), parallel=True, zoom=1.0, focus=None):
    """items: list of (shape, color, opacity). direction: vector from focus toward the camera."""
    ren = vtk.vtkRenderer()
    ren.SetBackground(1, 1, 1)
    ren.SetUseDepthPeeling(1)
    ren.SetMaximumNumberOfPeels(60)
    ren.SetOcclusionRatio(0.0)
    for shape, color, opacity in items:
        for a in actors_for(shape, color, opacity):
            ren.AddActor(a)
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetAlphaBitPlanes(1)
    win.SetMultiSamples(0)
    win.SetSize(*SIZE)
    win.AddRenderer(ren)

    cam = ren.GetActiveCamera()
    cam.SetParallelProjection(parallel)
    ren.ResetCamera()
    fp = focus or cam.GetFocalPoint()
    dist = 400.0
    cam.SetFocalPoint(*fp)
    cam.SetPosition(fp[0] + direction[0] * dist, fp[1] + direction[1] * dist, fp[2] + direction[2] * dist)
    cam.SetViewUp(*up)
    ren.ResetCamera()
    cam.Zoom(zoom)
    ren.ResetCameraClippingRange()

    ren.AddActor2D(text(title, 40, SIZE[1] - 70, 34, bold=True))
    for i, line in enumerate(notes):
        ren.AddActor2D(text(line, 40, SIZE[1] - 120 - 36 * i, 24, color=(0.25, 0.25, 0.25)))

    fxaa = vtk.vtkFXAAOptions()
    ren.SetUseFXAA(True)
    ren.SetFXAAOptions(fxaa)
    win.Render()
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(win)
    w2i.SetInputBufferTypeToRGB()
    w2i.ReadFrontBufferOff()
    w2i.Update()
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(path))
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()
    print("wrote", path.name)


def main():
    VIEWS.mkdir(parents=True, exist_ok=True)
    p = Params()
    L = Layout(p)
    holder = build_holder(p).val()
    bb = holder.BoundingBox()
    size_note = f"Holder envelope {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} mm   (finger axis x width x height)"

    xiao = ref.load_xiao(L)
    cam3660 = ref.ov3660(L, xiao["camera_cad"])
    headers = ref.header_strips(L)
    jumpers = ref.duponts(L)
    plug = ref.usb_plug(L)
    imu = ref.imu_board(L)
    pin = ref.gate_pin(L)
    finger = ref.finger(L)
    strap = ref.strap(L)

    solo = [(holder, HOLDER, 1.0)]
    assembled = [
        (holder, HOLDER, 1.0),
        (xiao["board"], BOARD, 1.0),
        (cam3660, CAMERA, 1.0),
        (headers, HEADER, 1.0),
        (jumpers, JUMPER, 1.0),
        (plug, PLUG, 1.0),
        (imu, IMU, 1.0),
        (pin, PIN, 1.0),
    ]
    ghost = [(holder, HOLDER, 0.38)] + assembled[1:]
    worn = assembled + [(finger, SKIN, 1.0), (strap, STRAP, 1.0)]

    hold_note = "Printed part only. Wrist / USB end is to the right (+X)."
    render(VIEWS / f"{NAME}_01_top.png", solo, (0, 0, 1), (0, 1, 0), "Top view (looking down at the open cradle)", (hold_note, size_note), zoom=0.9)
    render(VIEWS / f"{NAME}_02_side.png", solo, (0, -1, 0), (0, 0, 1), "Side view", (hold_note, size_note), zoom=0.9)
    render(VIEWS / f"{NAME}_03_underside.png", solo, (0, 0, -1), (0, -1, 0), "Underside (skin side)", ("Printed part only. Finger saddle groove runs along the finger. Wrist end to the right.", size_note), zoom=0.9)
    render(VIEWS / f"{NAME}_04_finger_mounted.png", worn, (0.55, -1.0, 0.50), (0, 0, 1), "Finger-mounted view",
           ("Orange = printed holder. Board = Seeed STEP. Camera, jumpers, plug, IMU, finger, strap = sized proxies (see dimension sheet).",
            f"Finger shown at an ASSUMED {p.finger_d:.0f} mm diameter. Fingertip to the left, wrist to the right."), parallel=False, zoom=1.25)
    render(VIEWS / f"{NAME}_05_iso_holder.png", solo, (-0.75, -1.0, 0.65), (0, 0, 1), "Holder, fingertip-end isometric", (hold_note.replace("right", "far right"), size_note), zoom=0.95)
    render(VIEWS / f"{NAME}_06_iso_holder_wrist_end.png", solo, (0.9, -0.8, 0.55), (0, 0, 1), "Holder, wrist-end isometric", ("Printed part only. Strain-relief nose toward the viewer.", size_note), zoom=0.95)
    render(VIEWS / f"{NAME}_07_assembled_side.png", ghost, (0, -1, 0), (0, 0, 1), "Assembled side view (holder shown translucent)",
           ("Board = Seeed STEP. Jumper housings, pins, USB plug, IMU, camera = nominal proxies, not measured.",), zoom=0.9)
    render(VIEWS / f"{NAME}_08_assembled_top.png", assembled, (0, 0, 1), (0, 1, 0), "Assembled top view", ("Buttons stay reachable beside the camera rest. Wrist / USB end to the right.",), zoom=0.9)
    render(VIEWS / f"{NAME}_09_assembled_wrist_end.png", ghost, (1, 0, 0), (0, 0, 1), "Assembled view from the wrist end (holder translucent)",
           ("USB plug proxy sits between the stop posts, above the strain-relief nose.",), zoom=0.9)


if __name__ == "__main__":
    main()
