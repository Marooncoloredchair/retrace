"""Build the SEND_TO_URI folder for the V2A first-fit print, plus the placement visual.

    .venv\\Scripts\\python.exe make_uri_package.py

Writes into ./SEND_TO_URI:
    ReTrace_Ring_V2A_current_hardware.step / .stl   (rebuilt from retrace_ring_v2.py)
    ReTrace_Ring_V2A_dimension_sheet.md
    V2A_view_1_top.png, V2A_view_2_side.png, V2A_view_3_palm_side.png   (labelled)
The lab note in that folder is written by hand and is left untouched.

Also writes ./out/V2/views/V2A_placement_on_hand.png (finger + thumb overview).
"""
from __future__ import annotations

import cadquery as cq
import vtk

import reference_models as ref
import reference_models_v2 as ref2
from render_views import BOARD, CAMERA, HEADER, HOLDER, IMU, JUMPER, PIN, PLUG, SKIN, STRAP, actors_for
from retrace_ring_v2 import HERE, OUT, VARIANTS, LayoutV2, build_holder_v2, dimension_sheet

NAME = "ReTrace_Ring_V2A_current_hardware"
SEND = HERE / "SEND_TO_URI"
SIZE = (1800, 1350)
THUMB = (0.90, 0.72, 0.62)
DIM = (0.78, 0.05, 0.05)
INK = (0.10, 0.10, 0.10)


# ---------------------------------------------------------------------------
# Labelled rendering
# ---------------------------------------------------------------------------
def _text(ren, msg, x, y, size=25, color=INK, bold=False, just="left", box=True):
    t = vtk.vtkTextActor()
    t.SetInput(msg)
    tp = t.GetTextProperty()
    tp.SetFontFamilyToArial()
    tp.SetFontSize(size)
    tp.SetColor(*color)
    tp.SetBold(bold)
    {"left": tp.SetJustificationToLeft, "right": tp.SetJustificationToRight, "center": tp.SetJustificationToCentered}[just]()
    tp.SetVerticalJustificationToCentered()
    if box:
        tp.SetBackgroundColor(1, 1, 1)
        tp.SetBackgroundOpacity(0.88)
    t.SetDisplayPosition(int(x), int(y))
    ren.AddActor2D(t)
    return t


def _label(ren, msg, lx, ly, tx, ty, w, h):
    """Centred text box kept inside the image, with a leader from the box edge to (tx, ty)."""
    t = _text(ren, msg, lx, ly, just="center")
    box = [0.0, 0.0]
    t.GetSize(ren, box)
    bw, bh = box[0] / 2 + 6, box[1] / 2 + 5
    lx = min(max(lx, bw + 14), w - bw - 14)
    ly = min(max(ly, bh + 14), h - bh - 14)
    t.SetDisplayPosition(int(lx), int(ly))
    dx, dy = tx - lx, ty - ly
    scale = min(bw / abs(dx) if dx else 1e9, bh / abs(dy) if dy else 1e9)
    if scale < 1.0:  # target is outside the text box
        _line(ren, [(lx + dx * scale, ly + dy * scale), (tx, ty)], INK, 2.0)
    _dot(ren, tx, ty)


def _line(ren, pts, color=INK, width=2.0):
    points = vtk.vtkPoints()
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(len(pts))
    for i, (x, y) in enumerate(pts):
        points.InsertNextPoint(x, y, 0)
        cells.InsertCellPoint(i)
    pd = vtk.vtkPolyData()
    pd.SetPoints(points)
    pd.SetLines(cells)
    m = vtk.vtkPolyDataMapper2D()
    m.SetInputData(pd)
    a = vtk.vtkActor2D()
    a.SetMapper(m)
    a.GetProperty().SetColor(*color)
    a.GetProperty().SetLineWidth(width)
    ren.AddActor2D(a)


def _dot(ren, x, y, r=6, color=INK):
    src = vtk.vtkRegularPolygonSource()
    src.SetNumberOfSides(20)
    src.SetRadius(r)
    src.SetCenter(x, y, 0)
    m = vtk.vtkPolyDataMapper2D()
    m.SetInputConnection(src.GetOutputPort())
    a = vtk.vtkActor2D()
    a.SetMapper(m)
    a.GetProperty().SetColor(*color)
    ren.AddActor2D(a)


def render_labeled(items, direction, up, title, notes=(), labels=(), dims=(), banners=(), zoom=1.0, size=SIZE):
    """Parallel-projection render with 2D labels.

    items   [(shape, colour, opacity)]
    labels  [(text, world_point, (fx, fy))]            text at a fraction of the image, leader to the point
    dims    [(world_p0, world_p1, text, (dx, dy))]     dimension line with end ticks, text offset in pixels
    banners [(text, (fx, fy))]                          plain bold text
    Returns the image as vtkImageData.
    """
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
    win.SetSize(*size)
    win.AddRenderer(ren)

    cam = ren.GetActiveCamera()
    cam.SetParallelProjection(True)
    ren.ResetCamera()
    fp = cam.GetFocalPoint()
    cam.SetPosition(fp[0] + direction[0] * 400, fp[1] + direction[1] * 400, fp[2] + direction[2] * 400)
    cam.SetViewUp(*up)
    ren.ResetCamera()
    cam.Zoom(zoom)
    ren.ResetCameraClippingRange()
    win.Render()

    def px(pt):
        c = vtk.vtkCoordinate()
        c.SetCoordinateSystemToWorld()
        c.SetValue(*pt)
        return c.GetComputedDoubleDisplayValue(ren)

    w, h = size
    for msg, target, (fx, fy) in labels:
        tx, ty = px(target)
        _label(ren, msg, fx * w, fy * h, tx, ty, w, h)
    for p0, p1, msg, (dx, dy) in dims:
        (x0, y0), (x1, y1) = px(p0), px(p1)
        length = max(1.0, ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
        nx, ny = -(y1 - y0) / length * 14, (x1 - x0) / length * 14
        _line(ren, [(x0, y0), (x1, y1)], DIM, 4.0)
        _line(ren, [(x0 - nx, y0 - ny), (x0 + nx, y0 + ny)], DIM, 4.0)
        _line(ren, [(x1 - nx, y1 - ny), (x1 + nx, y1 + ny)], DIM, 4.0)
        _text(ren, msg, (x0 + x1) / 2 + dx, (y0 + y1) / 2 + dy, size=30, color=DIM, bold=True, just="center")
    for msg, (fx, fy) in banners:
        _text(ren, msg, fx * w, fy * h, size=28, color=(0.30, 0.30, 0.30), bold=True, just="center", box=False)

    _text(ren, title, 40, h - 50, size=34, bold=True, box=False)
    for i, line in enumerate(notes):
        _text(ren, line, 40, h - 100 - 34 * i, size=23, color=(0.25, 0.25, 0.25), box=False)

    ren.SetUseFXAA(True)
    win.Render()
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(win)
    w2i.SetInputBufferTypeToRGB()
    w2i.ReadFrontBufferOff()
    w2i.Update()
    img = vtk.vtkImageData()
    img.DeepCopy(w2i.GetOutput())
    return img


def save_png(path, *images):
    writer = vtk.vtkPNGWriter()
    writer.SetFileName(str(path))
    if len(images) == 1:
        writer.SetInputData(images[0])
    else:
        app = vtk.vtkImageAppend()
        app.SetAppendAxis(0)
        for im in images:
            app.AddInputData(im)
        app.Update()
        writer.SetInputData(app.GetOutput())
    writer.Write()
    print("wrote", path)


def centre(shape):
    bb = shape.BoundingBox()
    return ((bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2)


# ---------------------------------------------------------------------------
def main():
    p, desc = VARIANTS[NAME]
    L = LayoutV2(p)
    rs, fs = L.rs, L.fs
    assert p.under_pcb_clearance >= 20.5, "V2A must keep room for the current header pins + Dupont jumpers"

    # ---- Printable files, rebuilt from source -------------------------------
    SEND.mkdir(exist_ok=True)
    body = build_holder_v2(p)
    holder = body.val()
    if len(body.solids().vals()) != 1 or not holder.isValid():
        raise SystemExit("V2A is not a single valid solid; nothing written")
    step, stl = SEND / f"{NAME}.step", SEND / f"{NAME}.stl"
    cq.exporters.export(body, str(step))
    holder.exportStl(str(stl), tolerance=0.01, angularTolerance=0.1)
    (SEND / "ReTrace_Ring_V2A_dimension_sheet.md").write_text(dimension_sheet(NAME, desc, p, holder), encoding="utf-8")
    back = cq.importers.importStep(str(step)).val()
    import trimesh

    mesh = trimesh.load_mesh(str(stl))
    ok = len(back.Solids()) == 1 and back.isValid() and abs(back.Volume() - holder.Volume()) < 0.5 and mesh.is_watertight
    bb = holder.BoundingBox()
    print(f"{NAME}: {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} mm, STEP re-import + STL watertight: {'ok' if ok else 'FAILED'}")
    if not ok:
        raise SystemExit("exported files failed the re-import check")

    # ---- Reference parts ------------------------------------------------------
    xiao = ref.load_xiao(L)
    board = xiao["board"]
    camera = ref.ov3660(L, xiao["camera_cad"])
    headers = ref.header_strips(L)
    jumpers = ref.duponts(L)
    plug = ref.usb_plug(L)
    imu = ref.imu_board(L)
    gate = ref.gate_pin(L)
    stub = ref2.retaining_stub(L)
    finger = ref2.finger(L)
    strap = ref2.strap(L)
    thumb = ref2.thumb(L)

    hardware = [(board, BOARD, 1.0), (camera, CAMERA, 1.0), (headers, HEADER, 1.0), (jumpers, JUMPER, 1.0),
                (plug, PLUG, 1.0), (gate, PIN, 1.0), (stub, PIN, 1.0)]
    worn = [(holder, HOLDER, 1.0)] + hardware + [(imu, IMU, 1.0), (finger, SKIN, 1.0), (strap, STRAP, 1.0)]
    ghost = [(holder, HOLDER, 0.30)] + hardware + [(imu, IMU, 1.0), (finger, SKIN, 1.0)]

    r = L.finger_r
    y_skin = L.y_fc + rs * r  # finger skin on the holder side
    y_wall = rs * L.y_out  # outer (thumb-side) face of the holder
    reach = abs(y_wall - y_skin)
    above_back = L.z_fc - r  # holder height above the back of the finger
    cam_c = centre(camera)
    imu_c = centre(imu)
    plug_c = centre(plug)
    board_pt = (-3.0, 0.0, L.z_pcb + 6.0)
    strap_x = (L.x_s0 + L.x_s1) / 2
    x_shelf_mid = (L.x_shelf_min + L.x_wall_min) / 2
    first_fit = "FIRST-FIT PROTOTYPE. Orange = printed part. Board = Seeed CAD. All other parts are unmeasured stand-ins."
    cam_label = "Camera (drawn from Seeed CAD of an\nolder camera - placement NOT verified)"

    # ---- 1. Top: looking at the back of the hand ---------------------------------
    img = render_labeled(
        worn, (0, 0, -1), (0, -rs, 0), "V2A  view 1 of 3:  TOP  (looking at the back of the hand)",
        (first_fit, f"Holder {bb.xlen:.0f} x {bb.ylen:.0f} x {bb.zlen:.0f} mm. Fingertip left, wrist right, thumb side toward the bottom."),
        labels=[
            ("Index finger (assumed 19 mm)", (-40, L.y_fc, 0), (0.22, 0.85)),
            ("Velcro strap, through the slot\nbehind the saddle", (strap_x, L.y_fc - rs * 3, 0), (0.66, 0.85)),
            ("Printed holder: closed cap over the\nheader pins and Dupont jumpers", (4, -rs * 5, 0), (0.25, 0.11)),
            ("MPU6050 shelf (the MPU6050 is on\nthe palm-facing side of the shelf)", (x_shelf_mid, rs * 4, 0), (0.17, 0.24)),
            ("Retaining stub pin", (L.x_stub, rs * (L.y_out + 1.2), L.z_stub), (0.53, 0.10)),
            ("USB-C plug and cable,\ntoward the wrist", plug_c, (0.84, 0.13)),
            ("Nose: cable-tie the\nUSB cable here", (L.x_usb_tip + 7, 0, 0), (0.87, 0.64)),
        ],
        dims=[((L.x_shelf_min - 9, y_skin, 0), (L.x_shelf_min - 9, y_wall, 0), f"{reach:.0f} mm beside\nthe finger", (-140, 0))],
        zoom=1.05,
    )
    save_png(SEND / "V2A_view_1_top.png", img)

    # ---- 2. Side: from the thumb side, holder translucent ----------------------
    y_cam = rs * 30
    img = render_labeled(
        ghost, (0, rs, 0), (0, 0, -1), "V2A  view 2 of 3:  SIDE  (from the thumb side, holder shown see-through)",
        (first_fit, "Back of the hand up, palm side down. Fingertip left, wrist right."),
        labels=[
            ("Dupont jumpers on the soldered header pins\n(20.5 mm allowed under the board)", (L.pin_x[2], y_cam, L.z_pcb - 10), (0.60, 0.84)),
            ("MPU6050 on its shelf, pins toward the palm", (imu_c[0], y_cam, 8), (0.20, 0.84)),
            ("XIAO ESP32S3 Sense: main board + camera board", (-2, y_cam, L.z_pcb + 3.0), (0.22, 0.20)),
            ("Retaining stub pin, between the two boards", (L.x_stub, y_cam, L.z_stub), (0.20, 0.30)),
            (cam_label, (cam_c[0], y_cam, cam_c[2] + 1), (0.56, 0.13)),
            ("USB-C plug and cable,\ntoward the wrist", (plug_c[0] + 8, y_cam, plug_c[2]), (0.87, 0.25)),
            ("Index finger (behind the holder)", (-38, y_cam, L.z_fc), (0.14, 0.50)),
            ("Strain-relief nose", (L.x_usb_tip + 9, y_cam, L.z_nose_top - 3), (0.86, 0.70)),
        ],
        dims=[
            ((L.x_wall_max + 34, y_cam, 0), (L.x_wall_max + 34, y_cam, bb.zmax), f"{bb.zlen:.0f} mm", (70, 0)),
            ((L.x_shelf_min - 6, y_cam, 0), (L.x_shelf_min - 6, y_cam, above_back), f"{above_back:.1f} mm above\nthe back of the finger", (-175, 40)),
        ],
        banners=[("BACK OF HAND", (0.50, 0.775)), ("PALM SIDE", (0.50, 0.06))],
        zoom=1.0,
    )
    save_png(SEND / "V2A_view_2_side.png", img)

    # ---- 3. Palm side: looking at the lens --------------------------------------
    palm_labels = [
        ("Index finger (assumed 19 mm)", (-40, L.y_fc, 40), (0.18, 0.10)),
        ("Velcro strap", (strap_x, L.y_fc - rs * 2, 40), (0.52, 0.10)),
        ("V saddle against the finger\n(solid wall, no contacts toward skin)", (L.x_s1 + 1.5, fs * (L.y_out + 3.5), 40), (0.80, 0.25)),
        ("MPU6050", (imu_c[0] + 3, imu_c[1] - rs * 4, 40), (0.10, 0.40)),
        ("XIAO ESP32S3 Sense\n(open side faces the palm)", (-4.5, rs * 4.0, 40), (0.40, 0.84)),
        (cam_label, (cam_c[0], cam_c[1], 40), (0.70, 0.84)),
        ("USB-C plug and cable,\ntoward the wrist", plug_c, (0.88, 0.70)),
    ]
    reach_dim = ((L.x_shelf_min - 9, y_skin, 40), (L.x_shelf_min - 9, y_wall, 40), f"{reach:.0f} mm beside\nthe finger", (-140, 0))
    img = render_labeled(
        worn, (0, 0, 1), (0, rs, 0), "V2A  view 3 of 3:  PALM SIDE  (looking at the camera lens)",
        (first_fit, "Fingertip left, wrist right. The holder sits beside the finger, on the thumb side."),
        labels=palm_labels + [("Retaining stub pin", (L.x_stub, rs * (L.y_out + 1.2), 40), (0.17, 0.78))],
        dims=[reach_dim], zoom=1.05,
    )
    save_png(SEND / "V2A_view_3_palm_side.png", img)

    # ---- Placement visual: finger + thumb, two panels ----------------------------
    panel = (1500, 1350)
    with_thumb = worn + [(thumb, THUMB, 1.0)]
    thumb_label = "Thumb, drawn spread to clear the holder\n(ILLUSTRATIVE - check on your own hand)"
    left = render_labeled(
        with_thumb, (0, 0, 1), (0, rs, 0), "Where V2A sits: seen from the PALM side",
        ("Fingertip left, wrist right. Camera looks toward you.", "First-fit prototype. Finger, thumb and camera positions are not measured."),
        labels=[
            ("Index finger", (-40, L.y_fc, 40), (0.20, 0.07)),
            ("Velcro strap", (strap_x, L.y_fc - rs * 2, 40), (0.55, 0.07)),
            ("MPU6050", (imu_c[0] + 3, imu_c[1], 40), (0.09, 0.42)),
            ("XIAO stack", (-4.5, rs * 4.0, 40), (0.20, 0.62)),
            ("Camera\n(placement NOT verified)", (cam_c[0], cam_c[1], 40), (0.36, 0.84)),
            ("USB-C to wrist", plug_c, (0.86, 0.36)),
            (thumb_label, (22, rs * 31, 40), (0.72, 0.84)),
        ],
        dims=[((L.x_shelf_min - 5, y_skin, 40), (L.x_shelf_min - 5, y_wall, 40), f"{reach:.0f} mm", (-75, 0))],
        zoom=0.95, size=panel,
    )
    end_items = [(holder, HOLDER, 1.0)] + hardware + [(finger, SKIN, 1.0), (strap, STRAP, 1.0), (thumb, THUMB, 1.0)]
    x_near = -70.0
    right = render_labeled(
        end_items, (-1, 0, 0), (0, 0, -1), "Seen from the FINGERTIP end",
        ("Back of the hand up, palm side down.", "MPU6050 not drawn here: it sits in front of the XIAO, at the fingertip end."),
        labels=[
            ("Index finger", (x_near, L.y_fc, L.z_fc), (0.13, 0.50)),
            ("Velcro strap", (x_near, L.y_fc - rs * (r + 0.8), L.z_fc + 1), (0.10, 0.36)),
            ("Header pins + Dupont jumpers\n(point to the back of the hand)", (x_near, rs * L.row_y, L.z_pcb - 9), (0.42, 0.80)),
            ("XIAO stack", (x_near, rs * 1.0, L.z_pcb + 4.6), (0.30, 0.20)),
            ("Camera lens, pointing out of\nthe palm side (NOT verified)", (x_near, cam_c[1], L.z_cam_top - 1.2), (0.52, 0.10)),
            ("Thumb (ILLUSTRATIVE)", (x_near, rs * 36, L.z_fc + 3), (0.84, 0.30)),
        ],
        dims=[
            ((x_near, y_skin, L.z_guard + 6), (x_near, y_wall, L.z_guard + 6), f"{reach:.0f} mm beside the finger", (0, -38)),
            ((x_near, rs * (L.y_out + 4), 0), (x_near, rs * (L.y_out + 4), above_back), f"{above_back:.1f} mm above the\nback of the finger", (190, 30)),
        ],
        banners=[("BACK OF HAND", (0.50, 0.865)), ("PALM SIDE", (0.50, 0.035))],
        zoom=0.95, size=panel,
    )
    views = OUT / "views"
    views.mkdir(parents=True, exist_ok=True)
    save_png(views / "V2A_placement_on_hand.png", left, right)


if __name__ == "__main__":
    main()
