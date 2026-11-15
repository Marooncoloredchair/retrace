"""Reference geometry placed in the holder frame. NOTHING here is printed.

Two kinds of reference:
  * Seeed's manufacturer STEP of the XIAO ESP32S3 Sense (real CAD).
  * Simple proxy solids for parts that have no manufacturer CAD here
    (header strips, Dupont jumper housings, USB plug, GY-521, finger, strap).
    Proxy sizes are nominal / assumed and are labelled as such.

Used by check_fit.py (interference checks) and render_views.py (screenshots).
"""
from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq

from retrace_ring_v1 import Layout, Params, box

HERE = Path(__file__).resolve().parent
XIAO_STEP = HERE / "reference" / "seeed_xiao_esp32s3_sense" / "Seeed Studio XIAO-ESP32-S3-Sense.step"

# Solid indices inside Seeed's STEP (verified by inspect_reference.py):
CAMERA_IDX = (100, 101, 102)  # OV2640 flex, body, lens glass as modelled by Seeed
SD_CARD_IDX = (72,)  # microSD card modelled in the slot (15 x 11 mm)

# Nominal proxy sizes (ASSUMED, standard parts).
HEADER_PLASTIC_H = 2.54
PIN_BELOW_PLASTIC = 6.1
PIN_SQ = 0.64
DUPONT_LEN = 14.0
DUPONT_SQ = 2.54
USB_PLUG_W, USB_PLUG_T, USB_PLUG_LEN = 12.4, 6.8, 20.0
IMU_L, IMU_W, IMU_T = 21.0, 16.0, 1.6  # GY-521, estimated


def _compound(shapes) -> cq.Shape:
    return cq.Compound.makeCompound(list(shapes))


def load_xiao(L: Layout) -> dict[str, cq.Shape]:
    """Seeed STEP split into board / camera / SD card, moved into the holder frame."""
    raw = cq.importers.importStep(str(XIAO_STEP)).val()
    solids = raw.Solids()
    pcb = max(solids, key=lambda s: s.Volume())
    bb = pcb.BoundingBox()
    assert abs(bb.xlen - L.p.pcb_len) < 0.01 and abs(bb.zlen - L.p.pcb_w) < 0.01, "unexpected STEP contents"
    xc, zc, y_bottom = (bb.xmin + bb.xmax) / 2, (bb.zmin + bb.zmax) / 2, bb.ymin

    def place(shape: cq.Shape) -> cq.Shape:
        # STEP axes: x = board length (USB at +x), y = up, z = board width.
        # Rotate +90 deg about X: (x, y, z) -> (x, -z, y), then centre and lift.
        s = shape.rotate(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), 90)
        return s.translate(cq.Vector(-xc, zc, -y_bottom + L.z_pcb))

    special = set(CAMERA_IDX) | set(SD_CARD_IDX)
    return {
        "board": place(_compound(s for i, s in enumerate(solids) if i not in special)),
        "camera_cad": place(_compound(solids[i] for i in CAMERA_IDX)),
        "sd_card": place(_compound(solids[i] for i in SD_CARD_IDX)),
        # Individual placed solids, keyed by their index in Seeed's STEP.
        "by_index": {i: place(s) for i, s in enumerate(solids)},
    }


def header_strips(L: Layout) -> cq.Shape:
    """Two 7-pin header strips soldered under the board, pins pointing down."""
    parts = []
    for s in (1, -1):
        y = s * L.row_y
        parts.append(box(L.x_strip_min, L.x_strip_max, y - 1.27, y + 1.27, L.z_pcb - HEADER_PLASTIC_H, L.z_pcb).val())
        for x in L.pin_x:
            parts.append(
                box(
                    x - PIN_SQ / 2, x + PIN_SQ / 2, y - PIN_SQ / 2, y + PIN_SQ / 2,
                    L.z_pcb - HEADER_PLASTIC_H - PIN_BELOW_PLASTIC, L.z_pcb + L.p.pin_top_above_pcb,
                ).val()
            )
    return _compound(parts)


# Pins wired in the firmware: IMU on D4/D5 + 3V3/GND, motor on D3 (+5V).
# Pin index 0 is nearest the fingertip. Which physical row is +Y does not matter here.
USED_PINS = {1: (1, 2, 3), -1: (4, 5, 6)}
ALL_PINS = {1: tuple(range(7)), -1: tuple(range(7))}


def duponts(L: Layout, pins=USED_PINS) -> cq.Shape:
    """Female Dupont housings plugged onto the header pins from below."""
    z1 = L.z_pcb - HEADER_PLASTIC_H
    parts = []
    for s, idxs in pins.items():
        y = s * L.row_y
        for i in idxs:
            x = L.pin_x[i]
            parts.append(box(x - DUPONT_SQ / 2, x + DUPONT_SQ / 2, y - DUPONT_SQ / 2, y + DUPONT_SQ / 2, z1 - DUPONT_LEN, z1).val())
    return _compound(parts)


def usb_plug(L: Layout) -> cq.Shape:
    """Typical USB-C plug overmold + cable stub. Size ASSUMED."""
    zc = L.z_pcb + L.p.usb_axis_h
    body = box(L.x_usb_tip, L.x_usb_tip + USB_PLUG_LEN, -USB_PLUG_W / 2, USB_PLUG_W / 2, zc - USB_PLUG_T / 2, zc + USB_PLUG_T / 2)
    body = body.edges("|X").fillet(2.5)
    cable = cq.Workplane("YZ", origin=(L.x_usb_tip + USB_PLUG_LEN, 0, zc)).circle(1.9).extrude(22)
    return body.union(cable).val()


def ov3660(L: Layout, camera_cad: cq.Shape) -> cq.Shape:
    """OV3660 proxy (8 x 8 x 5.3 per Seeed's drawing) at the position Seeed models the camera."""
    bb = camera_cad.BoundingBox()
    # Seeed's camera body is the 8 x 8 block at the USB end of the camera group.
    xc = bb.xmax - L.p.cam_body / 2
    yc = (bb.ymin + bb.ymax) / 2
    z0 = L.z_pcb + L.p.cam_seat_cad
    h = L.p.cam_body / 2
    base = box(xc - h, xc + h, yc - h, yc + h, z0, z0 + 2.6)
    lens = cq.Workplane("XY", origin=(xc, yc, z0 + 2.6)).circle(3.1).extrude(L.p.cam_h - 2.6)
    return base.union(lens).val()


def imu_board(L: Layout) -> cq.Shape:
    """GY-521 proxy lying on the shelf, header pins up with four jumpers. Size ESTIMATED."""
    x1 = L.x_wall_min - 0.5
    x0 = x1 - IMU_W
    z0 = L.z_shelf_top + 0.8  # foam tape
    parts = [box(x0, x1, -IMU_L / 2, IMU_L / 2, z0, z0 + IMU_T).val()]
    xh = x0 + 1.6
    parts.append(box(xh - 1.27, xh + 1.27, -IMU_L / 2 + 0.3, IMU_L / 2 - 0.3, z0 + IMU_T, z0 + IMU_T + HEADER_PLASTIC_H).val())
    for k in range(4):
        y = -IMU_L / 2 + 0.3 + 1.27 + k * 2.54
        zb = z0 + IMU_T + HEADER_PLASTIC_H
        parts.append(box(xh - 1.27, xh + 1.27, y - 1.27, y + 1.27, zb, zb + DUPONT_LEN).val())
    return _compound(parts)


def gate_pin(L: Layout) -> cq.Shape:
    """Stub of 1.75 mm filament pushed into the gate hole after the board is in."""
    p = L.p
    x = L.x_pcb_min - p.clr_end - p.gate_pin_d / 2
    return cq.Workplane("XY", origin=(x, 0, L.z_pcb - p.gate_pin_depth)).circle(0.875).extrude(p.gate_pin_depth + 2.5).val()


def finger_centre_z(L: Layout) -> float:
    r, w, d = L.p.finger_d / 2, L.p.saddle_w / 2, L.p.saddle_depth
    on_flat = d - r
    on_edges = -math.sqrt(r * r - w * w) if r > w else on_flat
    return min(on_flat, on_edges)


def finger(L: Layout, length: float = 95.0) -> cq.Shape:
    """Finger as a plain cylinder of the ASSUMED diameter. Visualisation only."""
    zc = finger_centre_z(L)
    return cq.Workplane("YZ", origin=(-length * 0.5, 0, zc)).circle(L.p.finger_d / 2).extrude(length).val()


def strap(L: Layout, thickness: float = 1.5) -> cq.Shape:
    """Velcro strap threaded through the slot and around the finger. Visualisation only."""
    p = L.p
    zc = finger_centre_z(L)
    r = p.finger_d / 2 + thickness / 2
    y_side = L.y_out + thickness / 2
    z_slot = L.z_bar_top + thickness / 2 + 0.1
    # Tangent from the point where the strap leaves the base side to the finger circle.
    py, pz = y_side, 0.0
    dist = math.hypot(py, pz - zc)
    theta = math.atan2(pz - zc, py)
    a = math.acos(min(1.0, r / dist))
    t0 = theta - a  # right-hand tangent point angle
    pts = [(-y_side, z_slot), (y_side, z_slot), (y_side, 0.0)]
    n = 40
    t_end = math.pi - t0 - 2 * math.pi  # mirrored tangent point, going clockwise under the finger
    for k in range(n + 1):
        t = t0 + (t_end - t0) * k / n
        pts.append((r * math.cos(t), zc + r * math.sin(t)))
    pts.append((-y_side, 0.0))
    origin = (-p.strap_w / 2, 0, 0)
    outer = cq.Workplane("YZ", origin=origin).polyline(pts).close().offset2D(thickness / 2).extrude(p.strap_w)
    inner = cq.Workplane("YZ", origin=origin).polyline(pts).close().offset2D(-thickness / 2).extrude(p.strap_w)
    return outer.cut(inner).val()


def export_context(p: Params | None = None) -> Path:
    """Write a coloured, named assembly STEP for design review. Reference only, not for printing."""
    from retrace_ring_v1 import NAME, OUT, build_holder

    p = p or Params()
    L = Layout(p)
    xiao = load_xiao(L)
    asm = cq.Assembly(name=f"{NAME}_fit_context")
    asm.add(build_holder(p).val(), name="PRINTED_holder", color=cq.Color(0.96, 0.55, 0.15))
    asm.add(xiao["board"], name="REF_xiao_sense_seeed_step", color=cq.Color(0.2, 0.45, 0.3))
    asm.add(ov3660(L, xiao["camera_cad"]), name="REF_ov3660_proxy_estimated_position", color=cq.Color(0.12, 0.12, 0.14))
    asm.add(header_strips(L), name="REF_header_strips_nominal", color=cq.Color(0.15, 0.15, 0.15))
    asm.add(duponts(L), name="REF_dupont_housings_nominal", color=cq.Color(0.75, 0.15, 0.15))
    asm.add(usb_plug(L), name="REF_usb_plug_assumed", color=cq.Color(0.82, 0.82, 0.85))
    if p.imu_shelf:
        asm.add(imu_board(L), name="REF_gy521_estimated", color=cq.Color(0.15, 0.3, 0.75))
    asm.add(gate_pin(L), name="REF_gate_pin_filament", color=cq.Color(0.95, 0.95, 0.3))
    out_dir = OUT / "reference_only"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{NAME}_fit_context_REFERENCE_ONLY.step"
    asm.save(str(path))
    return path


if __name__ == "__main__":
    print("wrote", export_context())
