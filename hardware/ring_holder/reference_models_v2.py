"""Reference / proxy geometry for the V2 side-saddle holder. NOTHING here is printed.

Finger, strap, retaining pin, lens field of view, and the IMU-on-cap proxy.
The board itself, header strips, jumpers and USB plug come from
reference_models.py (Seeed STEP + nominal proxies).
"""
from __future__ import annotations

import math

import cadquery as cq

from reference_models import HEADER_PLASTIC_H, IMU_L, IMU_T, IMU_W
from retrace_ring_v1 import box
from retrace_ring_v2 import LayoutV2

STRAP_T = 1.5
LENS_FOV_DIAG_DEG = 102.0  # Seeed OV3660 drawing: D = 102 deg, H = 85 deg


def finger(L: LayoutV2, toward_tip: float = 52.0, toward_wrist: float = 34.0) -> cq.Shape:
    """Straight finger of the ASSUMED diameter with a rounded tip. Visualisation only."""
    r = L.finger_r
    x0 = -toward_tip
    cyl = cq.Workplane("YZ", origin=(x0, L.y_fc, L.z_fc)).circle(r).extrude(toward_tip + toward_wrist)
    tip = cq.Workplane("XY").sphere(r).translate((x0, L.y_fc, L.z_fc))
    return cyl.union(tip).val()


def strap(L: LayoutV2) -> cq.Shape:
    """Velcro strap: behind the saddle plate, then around the finger. Visualisation only."""
    p = L.p
    t = STRAP_T
    rr = L.finger_r + t / 2
    o_back = L.o_gap - t / 2 - 0.15  # lying against the back of the saddle plate
    o_corner = L.o_tip + t / 2 + 0.1
    dz_out = L.hb + t / 2 + 0.1
    # Tangent from the lower outer corner of the saddle to the finger circle.
    dx, dz = o_corner - L.o_fc, -dz_out
    dist = math.hypot(dx, dz)
    theta = math.atan2(dz, dx)
    a = math.acos(min(1.0, rr / dist))
    t0 = theta + a  # tangent point on the dorsal side, heading round the far side
    t1 = -t0  # mirrored tangent point on the palm side
    pts_oz = [(o_back, dz_out), (o_back, -dz_out), (o_corner, -dz_out)]
    n = 48
    for k in range(n + 1):
        ang = t0 + (t1 - t0) * k / n
        pts_oz.append((L.o_fc + rr * math.cos(ang), rr * math.sin(ang)))
    pts_oz.append((o_corner, dz_out))
    pts = [(L.fs * (L.y_out + o), L.z_fc + dz) for o, dz in pts_oz]
    x0 = (L.x_s0 + L.x_s1) / 2 - p.strap_w / 2
    outer = cq.Workplane("YZ", origin=(x0, 0, 0)).polyline(pts).close().offset2D(t / 2).extrude(p.strap_w)
    inner = cq.Workplane("YZ", origin=(x0, 0, 0)).polyline(pts).close().offset2D(-t / 2).extrude(p.strap_w)
    return outer.cut(inner).val()


def retaining_stub(L: LayoutV2) -> cq.Shape:
    """Short 1.75 mm filament stub through the outer post, over the bare corner of the main PCB.

    Drawn at the bottom of its hole (closest to the board), with 1.5 mm left outside to hold.
    """
    y_a, y_b = L.rs * L.p.stub_tip_y, L.rs * (L.y_out + 1.5)
    z = L.z_stub - (L.p.ret_pin_d - 1.75) / 2
    return cq.Workplane("XZ", origin=(L.x_stub, max(y_a, y_b), z)).circle(0.875).extrude(abs(y_b - y_a)).val()


def thumb(L: LayoutV2) -> cq.Shape:
    """Thumb, spread to clear the holder. ILLUSTRATIVE ONLY: its real position depends on the hand."""
    r = 10.5
    a = cq.Vector(45.0, L.rs * 20.0, L.z_fc + 3.0)
    b = cq.Vector(-5.0, L.rs * 32.0, L.z_fc + 3.0)
    axis = b - a
    cyl = cq.Solid.makeCylinder(r, axis.Length, a, axis)
    tip = cq.Solid.makeSphere(r, b)
    return cq.Workplane("XY").add(cyl).union(cq.Workplane("XY").add(tip)).val()


def lens_fov(L: LayoutV2, camera: cq.Shape, length: float = 26.0) -> cq.Shape:
    """Cone showing the lens direction and Seeed's stated diagonal field of view."""
    bb = camera.BoundingBox()
    xc, yc = (bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2
    r2 = length * math.tan(math.radians(LENS_FOV_DIAG_DEG / 2))
    return cq.Solid.makeCone(0.05, r2, length, cq.Vector(xc, yc, L.z_cam_top), cq.Vector(0, 0, 1))


def imu_on_cap(L: LayoutV2, pin_len: float = 3.0) -> cq.Shape:
    """GY-521 proxy taped to the outside of the cap (V2B concept).

    Drawn with its header pins trimmed to pin_len, wires soldered on. Size ESTIMATED.
    """
    xc = (L.x_wall_min + L.x_wall_max) / 2
    z1 = -0.8  # foam tape
    z0 = z1 - IMU_T
    parts = [box(xc - IMU_L / 2, xc + IMU_L / 2, -IMU_W / 2, IMU_W / 2, z0, z1).val()]
    yh = L.rs * (IMU_W / 2 - 1.6)
    parts.append(box(xc - IMU_L / 2 + 0.3, xc + IMU_L / 2 - 0.3, yh - 1.27, yh + 1.27, z0 - HEADER_PLASTIC_H, z0).val())
    for k in range(8):
        x = xc - IMU_L / 2 + 0.3 + 1.27 + k * 2.54
        parts.append(box(x - 0.32, x + 0.32, yh - 0.32, yh + 0.32, z0 - HEADER_PLASTIC_H - pin_len, z0 - HEADER_PLASTIC_H).val())
    return cq.Compound.makeCompound(parts)
