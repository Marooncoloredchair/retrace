"""ReTrace Ring V1 - parametric first-fit finger cradle for the XIAO ESP32S3 Sense.

Real CAD (CadQuery / OpenCascade). Run:

    .venv\\Scripts\\python.exe retrace_ring_v1.py

Outputs (in ./out):
    ReTrace_Ring_V1.step            printable holder, one solid
    ReTrace_Ring_V1.stl             printable mesh
    ReTrace_Ring_V1_dimensions.md   dimension sheet generated from Params below

COORDINATES (holder frame, millimetres)
    +X  along the finger toward the WRIST (the USB-C end of the XIAO)
    -X  toward the fingertip
    +Y  across the finger
    +Z  away from the skin; Z = 0 is the lowest point of the holder (print bed)
    X = 0, Y = 0 is the centre of the XIAO main PCB.

EVERY dimension lives in Params. Each one carries a source tag:
    CAD      read from Seeed's STEP model (manufacturer model, NOT caliper-checked
             on the actual board)
    SPEC     read from Seeed's OV3660 module drawing
    USER     stated by the user
    ASSUMED  nominal / typical value for a standard part. NOT measured.
             Replace with a caliper measurement before trusting the fit.
    EST      estimated from community sources, no manufacturer drawing exists
    DESIGN   a design choice (wall, clearance, feature size)
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import cadquery as cq

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "V1_backup"  # V1 is kept as the backup design; V2 lives in out/V2
NAME = "ReTrace_Ring_V1"
BIG = 300.0


def P(value, src, note):
    return field(default=value, metadata={"src": src, "note": note})


@dataclass
class Params:
    # ---- XIAO ESP32S3 Sense stack, from Seeed's STEP -------------------------
    pcb_len: float = P(20.955, "CAD", "Main PCB length (USB axis).")
    pcb_w: float = P(17.78, "CAD", "Main PCB width.")
    pcb_t: float = P(1.25, "CAD", "Main PCB thickness as modelled by Seeed.")
    pcb_corner_r: float = P(1.9, "CAD", "Main PCB corner radius (approx. from arc bounding boxes).")
    header_row_spacing: float = P(15.24, "CAD", "Centre-to-centre spacing of the two 7-pin rows.")
    header_pitch: float = P(2.54, "CAD", "Pin pitch.")
    first_pin_from_tip_end: float = P(2.79, "CAD", "Fingertip-end PCB edge to centre of nearest pin.")
    usb_overhang: float = P(1.53, "CAD", "USB-C shell protrusion past the PCB edge.")
    usb_shell_w: float = P(8.94, "CAD", "USB-C shell width.")
    usb_shell_top: float = P(4.46, "CAD", "USB-C shell top above the PCB underside.")
    usb_axis_h: float = P(2.85, "CAD", "USB-C port centreline above the PCB underside.")
    exp_end_from_usb_end: float = P(6.58, "CAD", "Expansion-board edge to the USB-end PCB edge (expansion board is shorter).")
    exp_board_top: float = P(5.43, "CAD", "Expansion PCB top above the main PCB underside.")
    button_x_from_usb_end: float = P(2.89, "CAD", "Far edge of the BOOT/RESET buttons from the USB-end PCB edge.")
    button_y_inner: float = P(5.12, "CAD", "Inner edge of the buttons from the board centreline.")
    sd_card_underside: float = P(6.85, "CAD", "Underside of an inserted microSD card above the PCB underside (the card sits over the USB-C shell).")
    cam_seat_cad: float = P(8.20, "CAD", "Underside of the camera body above the PCB underside in Seeed's model, where it lies on the SD socket and card.")
    stack_top_cad: float = P(13.96, "CAD", "Top of the stack above the PCB underside in Seeed's model (OV2640 camera, not the OV3660).")

    # ---- OV3660 camera module, from Seeed's module drawing -------------------
    cam_body: float = P(8.0, "SPEC", "OV3660 module body, square.")
    cam_h: float = P(5.30, "SPEC", "OV3660 module total height, back to lens tip.")

    # ---- Standard parts on the user's board: NOT measured --------------------
    header_w: float = P(2.54, "ASSUMED", "Header plastic strip width (nominal 0.1 in).")
    under_pcb_clearance: float = P(
        20.5,
        "ASSUMED",
        "PCB underside to cradle floor = 2.54 header plastic + 14.0 Dupont housing + 4.0 wire bend. "
        "THE dominant height driver. Measure PCB underside to the lowest point of a plugged-in jumper, add 4.",
    )
    pin_top_above_pcb: float = P(3.3, "ASSUMED", "Tallest solder joint / pin stub above the PCB underside (camera-rest haunches clear this).")
    heatsink_relief_depth: float = P(7.0, "ASSUMED", "Depth of the open pocket under the board for a stick-on heat sink on the underside. 0 = none.")
    usb_stake_relief: float = P(1.0, "ASSUMED", "Relief under the board for the solder stubs of the USB-C shell legs (stubs are not in Seeed's CAD).")
    usb_plug_w_max: float = P(13.4, "ASSUMED", "Widest USB-C plug overmold the wrist-end posts allow (typical plugs are 12 to 12.5).")
    plug_rest_drop: float = P(2.5, "ASSUMED", "Plug-rest (strain-relief nose) top below the PCB underside. Clears overmolds up to about 10.7 thick.")
    strap_w: float = P(12.7, "USER", "Velcro strap width (1/2 inch).")
    strap_slot_h: float = P(3.0, "ASSUMED", "Strap slot height. Must exceed the thickness of one strap layer.")
    finger_d: float = P(19.0, "ASSUMED", "Finger diameter at the ring position. Only used to size the saddle and for the rendered finger.")
    imu_len: float = P(17.0, "EST", "IMU shelf length along the finger (GY-521 short side about 16).")

    # ---- Design choices -------------------------------------------------------
    clr_side: float = P(0.5, "DESIGN", "Board side clearance, each side. Loose on purpose (Seeed's own case uses 0.1).")
    clr_end: float = P(0.4, "DESIGN", "Board end clearance to the wrist-end stops.")
    clr_header_inner: float = P(1.1, "DESIGN", "Gap between the centre pedestal and the header/jumper row.")
    wall_t: float = P(1.6, "DESIGN", "Side wall thickness.")
    wall_above_pcb: float = P(0.3, "DESIGN", "Side wall height above the PCB top surface.")
    floor_t: float = P(1.4, "DESIGN", "Floor under the jumpers (roof of the strap slot).")
    bar_t: float = P(1.6, "DESIGN", "Strap bar thickness under the slot at the saddle centre.")
    saddle_w: float = P(12.0, "DESIGN", "Finger saddle groove width at the skin side.")
    saddle_depth: float = P(2.0, "DESIGN", "Finger saddle groove depth, 45-degree flanks. 0 = flat bottom.")
    strap_slot_extra: float = P(1.0, "DESIGN", "Strap slot length beyond the strap width.")
    corner_r: float = P(1.2, "DESIGN", "Outer vertical corner radius.")
    bottom_edge_r: float = P(0.8, "DESIGN", "Radius on the skin-side outer edges.")
    strap_edge_r: float = P(1.0, "DESIGN", "Radius where the strap leaves the slot.")
    gate_ext: float = P(3.5, "DESIGN", "Pedestal extension past the fingertip-end PCB edge (holds the gate pin).")
    gate_pin_d: float = P(2.1, "DESIGN", "Gate pin hole, sized for a stub of 1.75 filament or an M2 screw.")
    gate_pin_depth: float = P(8.0, "DESIGN", "Gate pin hole depth.")
    stop_t: float = P(1.6, "DESIGN", "Wrist-end stop post thickness along X.")
    tie_h: float = P(4.0, "DESIGN", "Height of the wrist-end tie bars and stops below the PCB underside.")
    tie_gap_to_strip: float = P(0.8, "DESIGN", "Gap between the header plastic end and the wrist-end tie bar.")
    window_margin_pins: float = P(9.5, "DESIGN", "Jumper window top below the PCB underside (keeps bare pin tips behind solid wall).")
    window_sill: float = P(2.5, "DESIGN", "Jumper window bottom above the floor.")
    window_x0: float = P(-8.0, "DESIGN", "Jumper window start (X).")
    window_x1: float = P(7.0, "DESIGN", "Jumper window end (X).")
    relief_x0: float = P(-8.0, "DESIGN", "Heat-sink relief start (X).")
    relief_x1: float = P(7.5, "DESIGN", "Heat-sink relief end (X).")
    deck_clear_over_usb: float = P(0.5, "DESIGN", "Camera rest underside above the USB-C shell.")
    deck_gap_under_sd: float = P(0.25, "DESIGN", "Camera rest top below the microSD card plane, so a card still slides in above it.")
    deck_gap_to_exp: float = P(0.6, "DESIGN", "Camera rest edge to the expansion board edge.")
    deck_tongue_half_w: float = P(4.6, "DESIGN", "Half width of the camera rest between the two button openings.")
    deck_rail2_w: float = P(1.1, "DESIGN", "Width of the outer camera-rest cross rail.")
    plug_face_gap: float = P(0.6, "DESIGN", "Camera rest and pedestal set back from the USB-C port mouth so the plug face never touches them.")
    land_half_w: float = P(3.4, "DESIGN", "Half width of the wrist-end support pad under the board (between the USB leg stubs).")
    deck_haunch: float = P(1.5, "DESIGN", "45-degree haunch under the camera-rest rails.")
    nose_len: float = P(14.0, "DESIGN", "Strain-relief nose length past the USB-C port mouth.")
    nose_half_w: float = P(4.5, "DESIGN", "Strain-relief nose half width.")
    nose_tip_t: float = P(4.5, "DESIGN", "Strain-relief nose thickness at the tip.")
    zip_slot_w: float = P(3.5, "DESIGN", "Cable-tie slot width.")
    zip_slot_h: float = P(1.6, "DESIGN", "Cable-tie slot height.")
    zip_slot_below_top: float = P(1.8, "DESIGN", "Material above the cable-tie slots.")
    imu_shelf: bool = P(True, "DESIGN", "Include the MPU6050 shelf at the fingertip end. False = compact holder.")
    imu_shelf_t: float = P(2.5, "DESIGN", "IMU shelf thickness.")
    imu_notch_w: float = P(3.0, "DESIGN", "Tie notch width in the IMU shelf edges.")
    imu_notch_d: float = P(1.2, "DESIGN", "Tie notch depth in the IMU shelf edges.")


class Layout:
    """Coordinates derived from Params. Shared by the model, checks and renders."""

    def __init__(self, p: Params):
        self.p = p
        self.z_bar_top = p.saddle_depth + p.bar_t
        self.z_slot_top = self.z_bar_top + p.strap_slot_h
        self.z_floor = self.z_slot_top + p.floor_t
        self.z_pcb = self.z_floor + p.under_pcb_clearance  # PCB underside
        self.z_wall_top = self.z_pcb + p.pcb_t + p.wall_above_pcb
        self.z_deck_bot = self.z_pcb + p.usb_shell_top + p.deck_clear_over_usb
        self.z_deck_top = self.z_pcb + p.sd_card_underside - p.deck_gap_under_sd
        self.cam_shim = p.cam_seat_cad - (p.sd_card_underside - p.deck_gap_under_sd)  # foam under the camera
        self.z_cam_top = self.z_pcb + p.cam_seat_cad + p.cam_h  # estimated OV3660 lens tip
        self.z_nose_top = self.z_pcb - p.plug_rest_drop
        self.z_shelf_top = self.z_floor + p.imu_shelf_t

        self.x_pcb_min = -p.pcb_len / 2
        self.x_pcb_max = p.pcb_len / 2
        self.x_stop = self.x_pcb_max + p.clr_end
        self.x_wall_max = self.x_stop + p.stop_t
        self.x_wall_min = self.x_pcb_min - p.gate_ext
        self.x_usb_tip = self.x_pcb_max + p.usb_overhang
        self.x_ped_max = self.x_usb_tip - p.plug_face_gap
        self.x_exp_end = self.x_pcb_max - p.exp_end_from_usb_end
        self.x_nose_end = self.x_usb_tip + p.nose_len
        self.x_shelf_min = self.x_wall_min - p.imu_len

        # Header pins: 7 per row, pin 0 nearest the fingertip end.
        self.pin_x = [self.x_pcb_min + p.first_pin_from_tip_end + i * p.header_pitch for i in range(7)]
        self.x_strip_min = self.pin_x[0] - p.header_pitch / 2
        self.x_strip_max = self.pin_x[-1] + p.header_pitch / 2
        self.row_y = p.header_row_spacing / 2

        self.y_in = p.pcb_w / 2 + p.clr_side  # inner face of the side walls
        self.y_out = self.y_in + p.wall_t
        self.y_ped = self.row_y - p.header_w / 2 - p.clr_header_inner
        self.y_stop_in = p.usb_plug_w_max / 2 + 0.5

        self.slot_len = p.strap_w + p.strap_slot_extra

        # Camera rest ("deck") over the USB-C shell.
        self.deck_x0 = self.x_exp_end + p.deck_gap_to_exp
        self.deck_x1 = self.x_usb_tip - p.plug_face_gap
        self.btn_x0 = self.x_pcb_max - p.button_x_from_usb_end - 0.4
        self.rail2_x0 = self.deck_x1 - p.deck_rail2_w
        self.tie_x0 = self.x_strip_max + p.tie_gap_to_strip

    def check(self):
        p = self.p
        deck_t = self.z_deck_top - self.z_deck_bot
        assert deck_t >= 0.8, f"camera rest too thin ({deck_t:.2f} mm)"
        assert self.btn_x0 - self.deck_x0 >= 1.5, "inner camera-rest rail too narrow"
        assert self.rail2_x0 - self.btn_x0 >= 2.0, "button opening too small"
        assert self.y_ped >= 3.0, "pedestal too narrow"
        assert p.under_pcb_clearance >= p.tie_h + 3.0, "under_pcb_clearance too small for the tie bars"
        assert self.y_stop_in < p.pcb_w / 2 - 0.3, "USB plug allowance leaves no stop for the PCB corners"
        assert p.saddle_w / 2 + 1.5 <= self.y_out, "saddle wider than the base"


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def box(x0, x1, y0, y1, z0, z1) -> cq.Workplane:
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    z0, z1 = sorted((z0, z1))
    return cq.Workplane("XY").box(x1 - x0, y1 - y0, z1 - z0, centered=False).translate((x0, y0, z0))


def both_sides(x0, x1, ya, yb, z0, z1) -> cq.Workplane:
    """The same box mirrored to +Y and -Y."""
    return box(x0, x1, ya, yb, z0, z1).union(box(x0, x1, -ya, -yb, z0, z1))


def rounded_prism(x0, x1, y_half, z0, z1, r) -> cq.Workplane:
    return box(x0, x1, -y_half, y_half, z0, z1).edges("|Z").fillet(r)


def profile_xz(points, y_half) -> cq.Workplane:
    """Extrude an X-Z polygon symmetrically to +/- y_half."""
    return cq.Workplane("XZ").polyline(points).close().extrude(y_half, both=True)


def profile_yz(points, x0, x1) -> cq.Workplane:
    """Extrude a Y-Z polygon from x0 to x1."""
    return cq.Workplane("YZ", origin=(x0, 0, 0)).polyline(points).close().extrude(x1 - x0)


def try_fillet(body: cq.Workplane, selector, radius: float, label: str) -> cq.Workplane:
    if radius <= 0:
        return body
    try:
        edges = body.edges(selector)
        if edges.size() == 0:
            print(f"  note: no edges found for fillet '{label}', skipped")
            return body
        return edges.fillet(radius)
    except Exception as exc:  # OpenCascade fillets can fail on odd parameter sets
        print(f"  note: fillet '{label}' skipped ({type(exc).__name__})")
        return body


# ---------------------------------------------------------------------------
# The holder
# ---------------------------------------------------------------------------
def build_holder(p: Params) -> cq.Workplane:
    L = Layout(p)
    L.check()

    # 1. Outer block: base + side walls, then open it up into an open-top cradle.
    body = rounded_prism(L.x_wall_min, L.x_wall_max, L.y_out, 0, L.z_wall_top, p.corner_r)
    body = try_fillet(body.faces("<Z"), None, p.bottom_edge_r, "skin-side edges")

    # Two jumper channels, open at both ends and at the top.
    body = body.cut(both_sides(-BIG, BIG, L.y_ped, L.y_in, L.z_floor, BIG))
    # Centre pedestal stops at the PCB underside.
    body = body.cut(box(-BIG, BIG, -L.y_ped, L.y_ped, L.z_pcb, BIG))
    # Past the USB port mouth the pedestal drops so the plug overmold clears it.
    body = body.cut(box(L.x_ped_max, BIG, -L.y_ped, L.y_ped, L.z_nose_top, BIG))

    # 2. Wrist end: tie bars under the PCB corners + stop posts for the PCB end.
    tie_h = min(p.tie_h, p.under_pcb_clearance - 3.0)
    upper = both_sides(L.tie_x0, L.x_ped_max, L.y_ped, L.y_in, L.z_pcb - tie_h, L.z_pcb)
    upper = upper.union(both_sides(L.x_stop, L.x_wall_max, L.y_stop_in, L.y_in, L.z_pcb - tie_h, L.z_wall_top))

    # 3. Camera rest: two cross rails + a centre tongue over the USB-C shell,
    #    leaving the BOOT/RESET buttons open from above and from the sides.
    deck = box(L.deck_x0, L.deck_x1, -L.y_out, L.y_out, L.z_deck_bot, L.z_deck_top)
    deck = deck.cut(both_sides(L.btn_x0, L.rail2_x0, p.deck_tongue_half_w, BIG, L.z_deck_bot - 1, L.z_deck_top + 1))
    legs = both_sides(L.deck_x0, L.btn_x0, L.y_in, L.y_out, L.z_wall_top - 0.01, L.z_deck_top)
    legs = legs.union(both_sides(L.rail2_x0, L.x_wall_max, L.y_in, L.y_out, L.z_wall_top - 0.01, L.z_deck_top))
    upper = upper.union(deck).union(legs)
    h = p.deck_haunch
    for x0, x1 in ((L.deck_x0, L.btn_x0), (L.rail2_x0, L.deck_x1)):
        for s in (1, -1):
            tri = [(s * L.y_in, L.z_deck_bot + 0.01), (s * (L.y_in - h), L.z_deck_bot + 0.01), (s * L.y_in, L.z_deck_bot - h)]
            upper = upper.union(profile_yz(tri, x0, x1))
    # Keep everything inside the rounded footprint.
    upper = upper.intersect(rounded_prism(L.x_wall_min, L.x_wall_max, L.y_out, 0, BIG, p.corner_r))
    body = body.union(upper)

    # 4. Strain-relief nose under the USB plug, with a 45-degree buttress so it
    #    prints without support.
    z_tip_bot = L.z_nose_top - p.nose_tip_t
    z_foot = max(L.z_floor, z_tip_bot - (L.x_nose_end - L.x_wall_max))
    x_root = L.x_ped_max - 1.0
    pts = [(x_root, L.z_floor), (L.x_wall_max, L.z_floor)]
    if z_foot > L.z_floor + 1e-6:
        pts.append((L.x_wall_max, z_foot))
    pts += [(L.x_nose_end, z_tip_bot), (L.x_nose_end, L.z_nose_top), (x_root, L.z_nose_top)]
    nose = profile_xz(pts, p.nose_half_w)
    nose = try_fillet(nose, cq.selectors.BoxSelector((L.x_nose_end - 0.1, -BIG, -BIG), (L.x_nose_end + 0.1, BIG, BIG)), 1.0, "nose tip")
    body = body.union(nose)

    # 5. Optional IMU shelf toward the fingertip, raised clear of the knuckle.
    if p.imu_shelf:
        shelf = box(L.x_shelf_min, L.x_wall_min + 1.5, -L.y_out, L.y_out, L.z_floor, L.z_shelf_top)
        gusset = profile_xz(
            [(L.x_wall_min + 1.5, 0), (L.x_wall_min + 1.5, L.z_floor), (L.x_wall_min - L.z_floor, L.z_floor), (L.x_wall_min, 0)],
            L.y_out,
        )
        shelf = shelf.union(gusset)
        shelf = shelf.intersect(rounded_prism(L.x_shelf_min, L.x_wall_max, L.y_out, 0, BIG, p.corner_r))
        # The shelf must not block the open jumper channels at the fingertip end.
        shelf = shelf.cut(both_sides(L.x_wall_min, BIG, L.y_ped, L.y_in, L.z_shelf_top, BIG))
        body = body.union(shelf)
        xc = (L.x_shelf_min + L.x_wall_min) / 2
        body = body.cut(
            both_sides(xc - p.imu_notch_w / 2, xc + p.imu_notch_w / 2, L.y_out - p.imu_notch_d, BIG, L.z_floor - 1, L.z_shelf_top + 1)
        )

    # 6. Cuts.
    # Strap slot: one lateral tunnel, its two mouths are the two Velcro slots.
    body = body.cut(box(-L.slot_len / 2, L.slot_len / 2, -BIG, BIG, L.z_bar_top, L.z_slot_top))
    # Finger saddle: trapezoid groove along the finger, 45-degree flanks.
    if p.saddle_depth > 0:
        w, d = p.saddle_w / 2, p.saddle_depth
        groove = [(-(w + 1), -1), (-(w - d), d), (w - d, d), (w + 1, -1)]
        body = body.cut(profile_yz(groove, -BIG, BIG))
    # Jumper windows in both side walls.
    z_w0, z_w1 = L.z_floor + p.window_sill, L.z_pcb - p.window_margin_pins
    if z_w1 - z_w0 >= 3.0:
        body = body.cut(both_sides(p.window_x0, p.window_x1, L.y_in - 0.2, BIG, z_w0, z_w1))
    # Heat-sink relief in the pedestal top. These pockets stop at the PCB plane:
    # the camera rest is directly above and must not be cut.
    z_cut_top = L.z_pcb + 0.01
    if p.heatsink_relief_depth > 0:
        body = body.cut(box(p.relief_x0, p.relief_x1, -L.y_ped - 0.01, L.y_ped + 0.01, L.z_pcb - p.heatsink_relief_depth, z_cut_top))
    # Wrist-end pad: keep only a centre strip so the board does not sit on the
    # solder stubs of the USB-C shell legs.
    if p.usb_stake_relief > 0:
        body = body.cut(
            both_sides(p.relief_x1 - 0.01, L.x_ped_max + 0.01, p.land_half_w, L.y_ped, L.z_pcb - p.usb_stake_relief, z_cut_top)
        )
    # Gate pin hole just past the fingertip-end PCB edge.
    x_pin = L.x_pcb_min - p.clr_end - p.gate_pin_d / 2
    pin = cq.Workplane("XY").circle(p.gate_pin_d / 2).extrude(p.gate_pin_depth + 1).translate((x_pin, 0, L.z_pcb - p.gate_pin_depth))
    body = body.cut(pin)
    # Cable-tie slots through the nose.
    z_s1 = L.z_nose_top - p.zip_slot_below_top
    for frac in (0.40, 0.80):
        xs = L.x_usb_tip + frac * p.nose_len
        body = body.cut(box(xs - p.zip_slot_w / 2, xs + p.zip_slot_w / 2, -BIG, BIG, z_s1 - p.zip_slot_h, z_s1))

    # 7. Round the edges the strap bears on.
    for s in (1, -1):
        sel = cq.selectors.BoxSelector(
            (-1, s * L.y_out - 0.2, L.z_bar_top - 0.2), (1, s * L.y_out + 0.2, L.z_bar_top + 0.2)
        )
        body = try_fillet(body, sel, p.strap_edge_r, f"strap edge {'+' if s > 0 else '-'}Y")

    return body


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
SRC_LABEL = {
    "CAD": "Seeed STEP (manufacturer CAD, not caliper-verified)",
    "SPEC": "Seeed OV3660 module drawing",
    "USER": "Stated by user",
    "ASSUMED": "ASSUMED - not measured",
    "EST": "ESTIMATED - no manufacturer data",
    "DESIGN": "Design choice",
}


def dimension_sheet(p: Params, solid: cq.Shape) -> str:
    L = Layout(p)
    bb = solid.BoundingBox()
    cam_top = L.z_cam_top
    lines = [
        f"# {NAME} - dimension sheet",
        "",
        "Generated by `retrace_ring_v1.py` from its `Params`. All values in millimetres.",
        "",
        "**No dimension on this sheet was measured on the physical hardware.** Values tagged",
        "CAD or SPEC come from Seeed's published files. Values tagged ASSUMED or ESTIMATED",
        "are nominal figures for standard parts and must be replaced by caliper measurements.",
        "",
        "## Result",
        "",
        "| Quantity | Value |",
        "| --- | --- |",
        f"| Holder bounding box (X along finger, Y across, Z tall) | {bb.xlen:.1f} x {bb.ylen:.1f} x {bb.zlen:.1f} |",
        f"| Footprint on the finger (length x width) | {L.x_wall_max - L.x_wall_min:.1f} x {2 * L.y_out:.1f} |",
        f"| Holder volume | {solid.Volume() / 1000:.2f} cm3 |",
        f"| PCB underside above the skin side | {L.z_pcb:.1f} |",
        f"| Estimated top of the OV3660 lens above the skin side | {cam_top:.1f} |",
        f"| Strap slot (through, length x height) | {L.slot_len:.1f} x {p.strap_slot_h:.1f} |",
        f"| Board pocket width between side walls | {2 * L.y_in:.2f} (board {p.pcb_w}) |",
        f"| Jumper channel width, each side | {L.y_in - L.y_ped:.2f} (header/jumper {p.header_w}) |",
        f"| Camera rest thickness / gap over USB shell | {L.z_deck_top - L.z_deck_bot:.2f} / {p.deck_clear_over_usb:.2f} |",
        f"| Foam shim needed between camera rest and camera (no SD card fitted) | about {L.cam_shim:.1f} |",
        "",
        "## Height stack (skin side up)",
        "",
        "| Layer | From | To | Thickness |",
        "| --- | --- | --- | --- |",
        f"| Finger saddle groove | 0.0 | {p.saddle_depth:.1f} | {p.saddle_depth:.1f} |",
        f"| Strap bar | {p.saddle_depth:.1f} | {L.z_bar_top:.1f} | {p.bar_t:.1f} |",
        f"| Strap slot | {L.z_bar_top:.1f} | {L.z_slot_top:.1f} | {p.strap_slot_h:.1f} |",
        f"| Floor (insulates jumpers from skin) | {L.z_slot_top:.1f} | {L.z_floor:.1f} | {p.floor_t:.1f} |",
        f"| Header pins + jumper housings + wire bend (ASSUMED) | {L.z_floor:.1f} | {L.z_pcb:.1f} | {p.under_pcb_clearance:.1f} |",
        f"| Main PCB (CAD) | {L.z_pcb:.1f} | {L.z_pcb + p.pcb_t:.2f} | {p.pcb_t:.2f} |",
        f"| Camera rest top (CAD-derived) | | {L.z_deck_top:.1f} | |",
        f"| OV3660 lens tip (CAD seat + SPEC height, unverified) | | {L.z_cam_top:.1f} | |",
        "",
        "## Reference models used",
        "",
        "| Part | Reference | Status |",
        "| --- | --- | --- |",
        "| XIAO ESP32S3 Sense (main + expansion board) | Seeed STEP `Seeed Studio XIAO-ESP32-S3-Sense.step`, 103 solids | Manufacturer CAD. Models the older OV2640 camera and has no header strips or solder stubs. |",
        "| OV3660 camera module | Seeed `OV3660_Camera_Module_Specification.pdf` drawing: body 8.0 x 8.0, height 5.30 | Manufacturer drawing. Resting position on this board is taken from where Seeed's STEP places the OV2640; not verified for the OV3660. |",
        "| Seeed printed enclosure | Seeed `XIAO-ESP32S3-Sense-housing-design (top/bottom).stp` | Inspected for wall and clearance practice only (2.1 walls, 1.5 floor, 0.1 to 0.2 clearance). |",
        "| GY-521 MPU6050 | No manufacturer drawing exists. Community CAD and listings agree on about 21 x 16 x 3.8, two 3 mm holes about 15.2 apart; some clones differ. | ESTIMATED bounding box only. The shelf is a plain 22 x 17 pad for tape or a tie. |",
        "| GODIYMODULES vibration module | None found | Not modelled. Stays off-finger in V1. |",
        "| Header strips, Dupont jumpers, USB-C plug, Velcro, finger | None | Nominal or assumed sizes, see ASSUMED rows below. |",
        "",
        "## Measurements that would replace assumptions",
        "",
        "| # | Measure on the real hardware | Replaces | Why it matters |",
        "| --- | --- | --- | --- |",
        "| 1 | PCB underside to the lowest point of a plugged-in jumper housing | `under_pcb_clearance` (that value + 4) | Sets the whole height of the holder. |",
        "| 2 | USB-C plug overmold width and thickness | `usb_plug_w_max`, `plug_rest_drop` | Plug must pass between the wrist-end posts and above the nose. |",
        "| 3 | Camera body position with the ribbon relaxed: distance from the USB-end board edge, height of its underside above the PCB, and whether a microSD card is fitted | `cam_seat_cad`, camera-rest position | Camera rest height and foam shim thickness. |",
        "| 4 | Heat sink on the underside of the XIAO: present? footprint and height | `heatsink_relief_depth`, `relief_x0/x1` | The board must not sit on it. |",
        "| 5 | GY-521 length x width, and whether its header is straight or right-angle and which face the pins leave | `imu_len`, shelf design | Decides whether the shelf works as drawn. |",
        "| 6 | Finger diameter (or circumference) where the ring sits, and Velcro thickness | `finger_d`, `saddle_w`, `strap_slot_h` | Saddle fit and strap slot. |",
        "| 7 | Main PCB length, width, thickness with calipers | `pcb_len`, `pcb_w`, `pcb_t` | Confirms Seeed's CAD against the actual board. |",
        "",
        "## Parameters",
        "",
        "| Parameter | Value | Source | Meaning |",
        "| --- | --- | --- | --- |",
    ]
    order = ["USER", "ASSUMED", "EST", "SPEC", "CAD", "DESIGN"]
    for src in order:
        for f in fields(p):
            if f.metadata["src"] != src:
                continue
            lines.append(f"| `{f.name}` | {getattr(p, f.name)} | {SRC_LABEL[src]} | {f.metadata['note']} |")
    lines.append("")
    return "\n".join(lines)


def export(p: Params, name: str = NAME, sheet: bool = True) -> cq.Shape:
    OUT.mkdir(parents=True, exist_ok=True)
    body = build_holder(p)
    solid = body.val()
    n_solids = len(body.solids().vals())
    bb = solid.BoundingBox()
    print(f"{name}: solids={n_solids} valid={solid.isValid()} volume={solid.Volume():.1f} mm3")
    print(f"  bounding box {bb.xlen:.2f} x {bb.ylen:.2f} x {bb.zlen:.2f} mm  (z {bb.zmin:.2f}..{bb.zmax:.2f})")
    if n_solids != 1 or not solid.isValid():
        raise SystemExit("holder is not a single valid solid; nothing exported")
    cq.exporters.export(body, str(OUT / f"{name}.step"))
    solid.exportStl(str(OUT / f"{name}.stl"), tolerance=0.01, angularTolerance=0.1)
    if sheet:
        (OUT / f"{name}_dimensions.md").write_text(dimension_sheet(p, solid), encoding="utf-8")
    print(f"  wrote {name}.step, {name}.stl{', ' + name + '_dimensions.md' if sheet else ''} -> {OUT}")
    return solid


if __name__ == "__main__":
    # Main deliverable: holder with the MPU6050 shelf.
    export(Params())
    # Same holder without the shelf (shorter), in case the IMU stays off-finger.
    export(Params(imu_shelf=False), f"{NAME}_noIMU", sheet=False)
