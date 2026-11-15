"""ReTrace Ring V2 - side-saddle cradle, camera lens facing the PALM side.

    .venv\\Scripts\\python.exe retrace_ring_v2.py

Why this layout
    The camera ribbon is too short to re-aim the camera, so the lens points
    along the board's top-face normal. For a palm-facing lens the board top
    must face the palm, which makes the soldered header pins (and whatever is
    plugged onto them) point toward the back of the hand. That column cannot
    pass through the finger, so the board is carried BESIDE the finger with
    the lens at the palm-side edge, instead of stacking everything on the palm.

Two holders from the same source, differing only in under_pcb_clearance:
    V2A  current hardware: header pins + female Dupont jumpers, nothing modified
    V2B  concept: needs a low-profile soldered wiring harness (NOT yet made)

HOLDER FRAME (also the print orientation, Z up, cap on the bed)
    +X  along the finger toward the wrist (USB-C end)
    +Z  toward the PALM side when worn (camera lens direction)
    Z=0 outer face of the cap (faces the back of the hand)
    The finger lies beside the holder at Y = finger_side * (...).

Source tags (CAD / SPEC / USER / ASSUMED / EST / DESIGN) are explained in
retrace_ring_v1.py. Nothing here has been measured on the physical hardware.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

import cadquery as cq

from retrace_ring_v1 import (
    BIG,
    HERE,
    SRC_LABEL,
    Layout,
    P,
    Params,
    both_sides,
    box,
    profile_xz,
    profile_yz,
    rounded_prism,
    try_fillet,
)

OUT = HERE / "out" / "V2"

# Fields inherited from the V1 parameter set that the V2 geometry does not use.
UNUSED_V1_FIELDS = {"saddle_w", "saddle_depth", "bar_t", "floor_t", "bottom_edge_r", "exp_board_top", "stack_top_cad"}

# Values that describe where the camera sits. They come from Seeed's CAD of a
# different camera (OV2640) and must be checked on the real OV3660.
CAMERA_FIELDS = {"cam_seat_cad", "cam_h", "cam_body", "guard_margin", "lens_proud"}


@dataclass
class ParamsV2(Params):
    # ---- Orientation on the hand (unverified assumptions) ---------------------
    finger_side: int = P(-1, "ASSUMED", "-1 = RIGHT index finger, holder on the thumb side. +1 mirrors it for the left hand.")
    lens_proud: float = P(0.0, "DESIGN", "How far the lens tip stands beyond the palm-side skin of the finger. 0 = flush.")

    # ---- More reference values from Seeed's STEP ------------------------------
    exp_board_underside: float = P(4.18, "CAD", "Underside of the camera expansion board above the main PCB underside (top of the gap between the two boards).")
    stub_tip_y: float = P(7.3, "CAD", "The retaining stub stops this far from the board centreline: over the bare PCB corner, short of the antenna and board-to-board connectors.")

    # ---- Design choices --------------------------------------------------------
    cap_t: float = P(1.5, "DESIGN", "Cap that closes the pin / wire space on the back-of-hand side.")
    guard_margin: float = P(0.7, "DESIGN", "Guard walls stand this far beyond the estimated lens tip.")
    guard_bar_h: float = P(2.0, "DESIGN", "Height of the outer guard bar.")
    # Board retention. A short filament stub enters from the outer side into the
    # gap BETWEEN the two stacked boards and sits over the bare corner of the main
    # PCB. Nothing crosses over the camera connector or the ribbon.
    stub_x_from_tip_end: float = P(0.9, "DESIGN", "Retaining-stub position from the fingertip-end PCB edge.")
    stub_gap: float = P(0.4, "DESIGN", "Gap between the main PCB top face and the retaining-stub hole.")
    ret_pin_d: float = P(2.1, "DESIGN", "Retaining-stub hole, for 1.75 filament.")
    ret_post_half_len: float = P(3.0, "DESIGN", "Half length of the outer post that carries the retaining stub.")
    saddle_x_from_tip_end: float = P(1.9, "DESIGN", "Centre of the fingertip-end saddle web, from the fingertip-end PCB edge.")
    v_plate_t: float = P(1.2, "DESIGN", "Saddle plate between the strap and the finger.")
    v_flat_half: float = P(3.0, "DESIGN", "Half height of the flat at the bottom of the V saddle.")
    v_open_half: float = P(8.0, "DESIGN", "Half height of the V saddle opening (45-degree flanks).")
    v_tip_r: float = P(0.8, "DESIGN", "Radius on the saddle tips.")
    fin_w: float = P(3.5, "DESIGN", "Width of each end web joining the saddle to the holder wall.")


class LayoutV2(Layout):
    def __init__(self, p: ParamsV2):
        super().__init__(p)
        # Height stack: cap, pin/wire space, PCB. (Replaces the V1 base stack.)
        self.z_floor = p.cap_t
        self.z_pcb = self.z_floor + p.under_pcb_clearance
        self.z_wall_top = self.z_pcb + p.pcb_t + p.wall_above_pcb
        self.z_deck_bot = self.z_pcb + p.usb_shell_top + p.deck_clear_over_usb
        self.z_deck_top = self.z_pcb + p.sd_card_underside - p.deck_gap_under_sd
        self.z_nose_top = self.z_pcb - p.plug_rest_drop
        self.z_cam_top = self.z_pcb + p.cam_seat_cad + p.cam_h  # estimated lens tip
        self.z_guard = self.z_cam_top + p.guard_margin
        self.z_shelf_top = p.imu_shelf_t

        self.fs = 1 if p.finger_side > 0 else -1  # side the finger is on
        self.rs = -self.fs  # outer side (toward the thumb)

        # Finger and V saddle. "o" = distance outward from the holder wall face.
        r = p.finger_d / 2
        self.finger_r = r
        self.z_fc = self.z_cam_top - p.lens_proud - r  # finger centre height
        self.hb = self.z_guard - self.z_fc  # saddle half height (top flush with the guard)
        self.v_open = min(p.v_open_half, self.hb - 1.5)
        self.o_gap = p.strap_slot_h
        self.o_flat = self.o_gap + p.v_plate_t
        self.o_tip = self.o_flat + (self.v_open - p.v_flat_half)
        self.o_fc = (self.o_flat - p.v_flat_half) + r * math.sqrt(2)
        self.y_fc = self.fs * (self.y_out + self.o_fc)
        self.z_saddle_bot = self.z_fc - self.hb

        # Saddle position along the finger (unchanged from the first V2 export).
        self.x_s0 = self.x_pcb_min + p.saddle_x_from_tip_end + p.fin_w / 2
        self.x_s1 = self.x_s0 + self.slot_len

        # Retaining stub: in the gap between the main PCB and the expansion board.
        self.x_stub = self.x_pcb_min + p.stub_x_from_tip_end
        self.z_stub = self.z_pcb + p.pcb_t + p.stub_gap + p.ret_pin_d / 2
        self.stub_gap_above = p.exp_board_underside - (p.pcb_t + p.stub_gap + p.ret_pin_d)

    def check(self):
        super().check()
        p = self.p
        assert self.stub_gap_above >= 0.3, "retaining stub hole does not fit between the two boards"
        assert self.v_open - p.v_flat_half >= 2.0, "V saddle too shallow for this finger size"
        assert self.finger_r * math.sqrt(0.5) <= self.v_open + 0.6, "finger rides on the saddle tips: increase v_open_half"
        assert self.x_s1 + p.fin_w <= self.x_wall_max, "saddle longer than the holder"
        assert self.z_saddle_bot >= 0, "saddle would go below the cap: increase lens_proud or finger_d"


def _saddle_point(L: LayoutV2, o: float, dz: float):
    return (L.fs * (L.y_out + o), L.z_fc + dz)


def build_holder_v2(p: ParamsV2) -> cq.Workplane:
    L = LayoutV2(p)
    L.check()
    fs, rs = L.fs, L.rs
    u = p.under_pcb_clearance
    envelope = rounded_prism(L.x_wall_min, L.x_wall_max, L.y_out, 0, BIG, p.corner_r)

    def side(sign, x0, x1, ya, yb, z0, z1):
        return box(x0, x1, sign * ya, sign * yb, z0, z1)

    # 1. Cap + two low side walls + centre pedestal (same board pocket as V1).
    body = rounded_prism(L.x_wall_min, L.x_wall_max, L.y_out, 0, L.z_wall_top, p.corner_r)
    body = body.cut(both_sides(-BIG, BIG, L.y_ped, L.y_in, L.z_floor, BIG))
    body = body.cut(box(-BIG, BIG, -L.y_ped, L.y_ped, L.z_pcb, BIG))
    body = body.cut(box(L.x_ped_max, BIG, -L.y_ped, L.y_ped, L.z_nose_top, BIG))

    # 2. Wrist end: tie bars under the PCB corners + stops for the PCB end.
    tie_h = min(p.tie_h, u - 3.0)
    upper = both_sides(L.tie_x0, L.x_ped_max, L.y_ped, L.y_in, L.z_pcb - tie_h, L.z_pcb)
    upper = upper.union(both_sides(L.x_stop, L.x_wall_max, L.y_stop_in, L.y_in, L.z_pcb - tie_h, L.z_wall_top))

    # 3. Camera rest over the USB-C shell (camera is taped to it).
    deck = box(L.deck_x0, L.deck_x1, -L.y_out, L.y_out, L.z_deck_bot, L.z_deck_top)
    deck = deck.cut(both_sides(L.btn_x0, L.rail2_x0, p.deck_tongue_half_w, BIG, L.z_deck_bot - 1, L.z_deck_top + 1))
    upper = upper.union(deck)
    h = p.deck_haunch
    for x0, x1 in ((L.deck_x0, L.btn_x0), (L.rail2_x0, L.deck_x1)):
        for s in (1, -1):
            tri = [(s * L.y_in, L.z_deck_bot + 0.01), (s * (L.y_in - h), L.z_deck_bot + 0.01), (s * L.y_in, L.z_deck_bot - h)]
            upper = upper.union(profile_yz(tri, x0, x1))

    # 4. Finger-side wall runs full height: it carries the saddle and guards the lens.
    upper = upper.union(side(fs, L.x_wall_min, L.x_wall_max, L.y_in, L.y_out, L.z_wall_top - 0.01, L.z_guard))

    # 5. Outer side: two posts + a bar guard the lens and carry the camera rest;
    #    the opening between the posts keeps that side's button reachable.
    z0 = L.z_wall_top - 0.01
    upper = upper.union(side(rs, L.deck_x0, L.btn_x0, L.y_in, L.y_out, z0, L.z_guard))
    upper = upper.union(side(rs, L.rail2_x0, L.x_wall_max, L.y_in, L.y_out, z0, L.z_guard))
    upper = upper.union(side(rs, L.deck_x0, L.x_wall_max, L.y_in, L.y_out, L.z_guard - p.guard_bar_h, L.z_guard))
    # Low outer post for the retaining stub at the fingertip end.
    z_post = L.z_stub + p.ret_pin_d / 2 + 1.6
    upper = upper.union(side(rs, L.x_stub - p.ret_post_half_len, L.x_stub + p.ret_post_half_len, L.y_in, L.y_out, z0, z_post))

    upper = upper.intersect(envelope)
    body = body.union(upper)

    # 6. V saddle for the finger, standing off the wall so the strap passes behind it.
    hb, vo, vf = L.hb, L.v_open, p.v_flat_half
    prof = [(-0.2, -hb), (L.o_tip, -hb), (L.o_tip, -vo), (L.o_flat, -vf), (L.o_flat, vf), (L.o_tip, vo), (L.o_tip, hb), (-0.2, hb)]
    x_a, x_b = L.x_s0 - p.fin_w, L.x_s1 + p.fin_w
    saddle = profile_yz([_saddle_point(L, o, dz) for o, dz in prof], x_a, x_b)
    xm = (x_a + x_b) / 2
    for dz in (-hb, -vo, vo, hb):
        y, z = _saddle_point(L, L.o_tip, dz)
        sel = cq.selectors.BoxSelector((xm - 1, y - 0.1, z - 0.1), (xm + 1, y + 0.1, z + 0.1))
        saddle = try_fillet(saddle, sel, p.v_tip_r, "saddle tip")
    # 45-degree webs under both ends of the saddle so it prints without support.
    zb = L.z_saddle_bot
    reach = L.o_tip + 0.2
    if zb > 0.3:
        if zb - reach >= 0:
            web = [(-0.2, zb - reach), (L.o_tip, zb + 0.01), (-0.2, zb + 0.01)]
        else:
            web = [(-0.2, 0), (L.o_tip - zb, 0), (L.o_tip, zb + 0.01), (-0.2, zb + 0.01)]
        web_pts = [(fs * (L.y_out + o), z) for o, z in web]
        for x0, x1 in ((x_a, L.x_s0), (L.x_s1, x_b)):
            saddle = saddle.union(profile_yz(web_pts, x0, x1))
    body = body.union(saddle)

    # 7. Strain-relief nose under the USB plug. Sits on the bed when the holder is low.
    z_top = L.z_nose_top
    z_tip_bot = z_top - p.nose_tip_t
    x_root, x_end = L.x_ped_max - 1.0, L.x_nose_end
    if z_tip_bot <= 0.2:
        pts = [(x_root, 0), (x_end, 0), (x_end, z_top), (x_root, z_top)]
    else:
        z_foot = z_tip_bot - (x_end - L.x_wall_max)
        if z_foot >= 0:
            pts = [(x_root, 0), (L.x_wall_max, 0), (L.x_wall_max, z_foot), (x_end, z_tip_bot), (x_end, z_top), (x_root, z_top)]
        else:
            pts = [(x_root, 0), (x_end - z_tip_bot, 0), (x_end, z_tip_bot), (x_end, z_top), (x_root, z_top)]
    nose = profile_xz(pts, p.nose_half_w)
    nose = try_fillet(nose, cq.selectors.BoxSelector((x_end - 0.1, -BIG, 0.5), (x_end + 0.1, BIG, BIG)), 1.0, "nose tip")
    body = body.union(nose)

    # 8. Optional IMU shelf at the fingertip end, flat on the bed (no support needed).
    if p.imu_shelf:
        shelf = box(L.x_shelf_min, L.x_wall_min + 1.5, -L.y_out, L.y_out, 0, p.imu_shelf_t)
        shelf = shelf.intersect(rounded_prism(L.x_shelf_min, L.x_wall_max, L.y_out, 0, BIG, p.corner_r))
        body = body.union(shelf)
        xc = (L.x_shelf_min + L.x_wall_min) / 2
        body = body.cut(both_sides(xc - p.imu_notch_w / 2, xc + p.imu_notch_w / 2, L.y_out - p.imu_notch_d, BIG, -1, p.imu_shelf_t + 1))

    # 9. Cuts.
    # Strap slot: vertical through-slot between the holder wall and the saddle plate.
    body = body.cut(side(fs, L.x_s0, L.x_s1, L.y_out, L.y_out + L.o_gap, -1, BIG))
    # Jumper window, outer wall only (the finger-side wall stays solid).
    z_w0, z_w1 = L.z_floor + p.window_sill, L.z_pcb - p.window_margin_pins
    if z_w1 - z_w0 >= 3.0:
        body = body.cut(side(rs, p.window_x0, p.window_x1, L.y_in - 0.2, BIG, z_w0, z_w1))
    # Pockets under the board; they stop at the PCB plane (camera rest is above).
    z_cut_top = L.z_pcb + 0.01
    relief = min(p.heatsink_relief_depth, u - 2.0)
    if relief > 0:
        body = body.cut(box(p.relief_x0, p.relief_x1, -L.y_ped - 0.01, L.y_ped + 0.01, L.z_pcb - relief, z_cut_top))
    if p.usb_stake_relief > 0:
        body = body.cut(both_sides(p.relief_x1 - 0.01, L.x_ped_max + 0.01, p.land_half_w, L.y_ped, L.z_pcb - p.usb_stake_relief, z_cut_top))
    # Gate pin hole (stops the board sliding back out toward the fingertip).
    gate_depth = min(p.gate_pin_depth, u + p.cap_t - 1.0)
    x_pin = L.x_pcb_min - p.clr_end - p.gate_pin_d / 2
    body = body.cut(cq.Workplane("XY").circle(p.gate_pin_d / 2).extrude(gate_depth + 1).translate((x_pin, 0, L.z_pcb - gate_depth)))
    # Retaining stub hole: through the outer post only. The finger-side wall is
    # not drilled, and nothing passes over the camera connector or ribbon.
    y_a, y_b = rs * (L.y_in - 0.3), rs * (L.y_out + 1.0)
    hole = cq.Workplane("XZ", origin=(L.x_stub, max(y_a, y_b), L.z_stub)).circle(p.ret_pin_d / 2).extrude(abs(y_b - y_a))
    body = body.cut(hole)
    # Cable-tie slots through the nose.
    z_s1 = L.z_nose_top - p.zip_slot_below_top
    for frac in (0.40, 0.80):
        xs = L.x_usb_tip + frac * p.nose_len
        body = body.cut(box(xs - p.zip_slot_w / 2, xs + p.zip_slot_w / 2, -BIG, BIG, z_s1 - p.zip_slot_h, z_s1))

    # 10. Round the two edges the strap bends over.
    xm = (L.x_s0 + L.x_s1) / 2
    for dz in (-hb, hb):
        y, z = _saddle_point(L, L.o_gap, dz)
        sel = cq.selectors.BoxSelector((xm - 1, y - 0.1, z - 0.1), (xm + 1, y + 0.1, z + 0.1))
        body = try_fillet(body, sel, p.strap_edge_r, "strap edge")

    return body


# ---------------------------------------------------------------------------
# Variants and export
# ---------------------------------------------------------------------------
VARIANTS = {
    # name: (params, one-line description)
    "ReTrace_Ring_V2A_current_hardware": (
        ParamsV2(),
        "CURRENT hardware, nothing modified: header pins + female Dupont jumpers. With MPU6050 shelf.",
    ),
    "ReTrace_Ring_V2A_current_hardware_noIMU": (
        ParamsV2(imu_shelf=False),
        "CURRENT hardware, nothing modified. No MPU6050 shelf (17 mm shorter).",
    ),
    "ReTrace_Ring_V2B_low_profile_harness_CONCEPT": (
        ParamsV2(under_pcb_clearance=10.0, imu_shelf=False),
        "CONCEPT: requires a soldered low-profile wiring harness (wires soldered to the intact header pins). MPU6050 tapes to the cap.",
    ),
}


def dimension_sheet(name: str, desc: str, p: ParamsV2, solid: cq.Shape) -> str:
    L = LayoutV2(p)
    bb = solid.BoundingBox()
    r = L.finger_r
    back_of_finger = L.z_fc - r
    lines = [
        f"# {name} - dimension sheet",
        "",
        desc,
        "",
        "Generated by `retrace_ring_v2.py`. Millimetres. **Nothing on this sheet was measured on the",
        "physical hardware.** CAD / SPEC values come from Seeed's published files; ASSUMED and",
        "ESTIMATED values are nominal and must be confirmed with calipers.",
        "",
        "## Orientation (unverified)",
        "",
        f"- Worn on the **{'right' if L.fs < 0 else 'left'} index finger**, holder on the **thumb side** of the finger (ASSUMED hand; `finger_side` mirrors it).",
        "- USB-C toward the wrist. Camera lens points away from the **palm side** of the finger.",
        f"- Lens tip is set {p.lens_proud:.1f} mm beyond the palm-side skin plane of an ASSUMED {p.finger_d:.0f} mm finger.",
        "",
        "## Camera placement: NEEDS PHYSICAL VERIFICATION",
        "",
        "The camera is drawn where Seeed's CAD places an older camera (OV2640). The OV3660 on this board,",
        "on its short ribbon, has not been measured, so its position, height and lens direction are not known.",
        f"The holder assumes the lens tip is {p.cam_seat_cad + p.cam_h:.1f} mm above the main PCB underside and that the camera",
        "can be taped to the rest over the USB-C shell with the ribbon relaxed. Parameters marked",
        "`camera: verify` below depend on this.",
        "",
        "## Result",
        "",
        "| Quantity | Value |",
        "| --- | --- |",
        f"| Holder bounding box: along finger x across x thickness (back of hand to palm) | {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} |",
        f"| Holder body without nose / shelf / saddle (length x width) | {L.x_wall_max - L.x_wall_min:.1f} x {2 * L.y_out:.1f} |",
        f"| Holder volume | {solid.Volume() / 1000:.2f} cm3 |",
        f"| Space under the board for pins and wiring (`under_pcb_clearance`) | {p.under_pcb_clearance:.1f} |",
        f"| Estimated lens tip above the cap face | {L.z_cam_top:.1f} |",
        f"| Holder stands above the back of the finger by | {max(0.0, back_of_finger):.1f} |",
        f"| Holder stands beyond the palm-side skin by (guard walls) | {L.z_guard - (L.z_fc + r):.1f} |",
        f"| Finger skin to the holder wall | {L.o_fc - r:.1f} |",
        f"| Holder reaches sideways from the finger skin (thumb side) | {L.o_fc - r + 2 * L.y_out:.1f} |",
        f"| Strap slot (length x gap) | {L.slot_len:.1f} x {L.o_gap:.1f} |",
        f"| Board pocket width between walls | {2 * L.y_in:.2f} (board {p.pcb_w}) |",
        f"| Retaining stub hole: above the main PCB top / below the expansion board (Seeed CAD) | {p.stub_gap:.1f} / {L.stub_gap_above:.2f} |",
        f"| Foam shim between camera rest and camera (no SD card fitted) | about {L.cam_shim:.1f} |",
        "",
        "## Parameters",
        "",
        "| Parameter | Value | Source | Meaning |",
        "| --- | --- | --- | --- |",
    ]
    for src in ["USER", "ASSUMED", "EST", "SPEC", "CAD", "DESIGN"]:
        for f in fields(p):
            if f.metadata["src"] != src or f.name in UNUSED_V1_FIELDS:
                continue
            label = SRC_LABEL[src] + (" - **camera: verify**" if f.name in CAMERA_FIELDS else "")
            lines.append(f"| `{f.name}` | {getattr(p, f.name)} | {label} | {f.metadata['note']} |")
    lines.append("")
    return "\n".join(lines)


def export_all():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (p, desc) in VARIANTS.items():
        body = build_holder_v2(p)
        solid = body.val()
        n = len(body.solids().vals())
        bb = solid.BoundingBox()
        print(f"{name}: solids={n} valid={solid.isValid()} volume={solid.Volume():.1f} mm3")
        print(f"  bounding box {bb.xlen:.2f} x {bb.ylen:.2f} x {bb.zlen:.2f} mm")
        if n != 1 or not solid.isValid():
            raise SystemExit(f"{name} is not a single valid solid; nothing exported")
        cq.exporters.export(body, str(OUT / f"{name}.step"))
        solid.exportStl(str(OUT / f"{name}.stl"), tolerance=0.01, angularTolerance=0.1)
        (OUT / f"{name}_dimensions.md").write_text(dimension_sheet(name, desc, p, solid), encoding="utf-8")
    print(f"wrote STEP, STL and dimension sheets -> {OUT}")


if __name__ == "__main__":
    export_all()
