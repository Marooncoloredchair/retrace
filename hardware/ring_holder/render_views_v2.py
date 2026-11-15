"""Render PNG views of the V2 holders, shown worn on a finger (offscreen VTK).

    .venv\\Scripts\\python.exe render_views_v2.py

Writes PNGs to ./out/V2/views and a reference-only assembly STEP per variant.
Orange = the printed holder. Board = Seeed's STEP. Everything else is a sized
proxy (see the dimension sheets); the finger is an ASSUMED-diameter cylinder.
"""
from __future__ import annotations

import cadquery as cq

import reference_models as ref
import reference_models_v2 as ref2
from render_views import BOARD, CAMERA, HEADER, HOLDER, IMU, JUMPER, PIN, PLUG, SKIN, STRAP, render
from retrace_ring_v1 import Layout, Params, build_holder
from retrace_ring_v2 import OUT, VARIANTS, LayoutV2, build_holder_v2

VIEWS = OUT / "views"
FOV = (0.35, 0.65, 0.95)
SHORT = {
    "ReTrace_Ring_V2A_current_hardware": "V2A",
    "ReTrace_Ring_V2A_current_hardware_noIMU": "V2A_noIMU",
    "ReTrace_Ring_V2B_low_profile_harness_CONCEPT": "V2B_CONCEPT",
}
DORSAL_UP = (0, 0, -1)


def parts_for(p):
    """Named reference parts for one variant: {name: (shape, colour)}."""
    L = LayoutV2(p)
    xiao = ref.load_xiao(L)
    uses_duponts = p.under_pcb_clearance >= 18.0
    parts = {
        "PRINTED_holder": (build_holder_v2(p).val(), HOLDER),
        "REF_xiao_sense_stack_seeed_step": (xiao["board"], BOARD),
        "REF_ov3660_proxy_POSITION_NOT_VERIFIED": (ref.ov3660(L, xiao["camera_cad"]), CAMERA),
        "REF_header_strips_and_pins_nominal": (ref.header_strips(L), HEADER),
        "REF_usb_c_plug_assumed": (ref.usb_plug(L), PLUG),
        "REF_gate_pin_filament": (ref.gate_pin(L), PIN),
        "REF_retaining_stub_filament": (ref2.retaining_stub(L), PIN),
    }
    if uses_duponts:
        parts["REF_dupont_housings_nominal"] = (ref.duponts(L), JUMPER)
    if p.imu_shelf:
        parts["REF_gy521_estimated_on_shelf"] = (ref.imu_board(L), IMU)
    elif not uses_duponts:
        parts["REF_gy521_estimated_taped_to_cap"] = (ref2.imu_on_cap(L), IMU)
    parts["REF_finger_assumed_diameter"] = (ref2.finger(L), SKIN)
    parts["REF_strap"] = (ref2.strap(L), STRAP)
    return L, parts, ref2.lens_fov(L, xiao["camera_cad"])


def export_context(name, parts):
    asm = cq.Assembly(name=f"{name}_fit_context")
    for part_name, (shape, color) in parts.items():
        asm.add(shape, name=part_name, color=cq.Color(*color))
    out_dir = OUT / "reference_only"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}_on_finger_REFERENCE_ONLY.step"
    asm.save(str(path))
    print("wrote", path.name)


def render_variant(name, p, desc):
    L, parts, fov = parts_for(p)
    tag = SHORT[name]
    holder = parts["PRINTED_holder"][0]
    bb = holder.BoundingBox()
    hand = "right" if L.fs < 0 else "left"
    size = f"Holder {bb.xlen:.0f} x {bb.ylen:.0f} x {bb.zlen:.0f} mm (along finger x sideways x back-of-hand to palm). Finger: ASSUMED {p.finger_d:.0f} mm, {hand} hand."
    proxy = "Orange = printed holder. Board = Seeed STEP. Pins, jumpers, plug, IMU, finger, strap = unmeasured proxies. Camera position NOT verified."

    worn = [(shape, color, 1.0) for shape, color in parts.values()]
    ghost = [(shape, color, 0.30 if n == "PRINTED_holder" else 1.0) for n, (shape, color) in parts.items() if n != "REF_strap"]
    solo = [(holder, HOLDER, 1.0)]

    def out(n, label):
        return VIEWS / f"{tag}_{n:02d}_{label}.png"

    render(out(1, "on_finger_top"), worn, (0, 0, -1), (0, -L.rs, 0), f"{tag}: on finger, TOP (looking at the back of the hand)",
           (size, "Fingertip left, wrist right, thumb side toward the bottom. The holder sits beside the finger.", proxy), zoom=1.45)
    render(out(2, "on_finger_side"), ghost, (0, L.rs, 0), DORSAL_UP, f"{tag}: on finger, SIDE (from the thumb side, holder translucent)",
           (size, "Back of the hand up, palm side down. Lens points down (palm-ward). Header pins point up toward the back of the hand.", proxy), zoom=1.45)
    render(out(3, "on_finger_palm"), worn, (0, 0, 1), (0, L.rs, 0), f"{tag}: on finger, PALM SIDE (looking at the lens)",
           (size, "Fingertip left, wrist right. The lens sits at the palm-side edge of the finger and looks toward the viewer.", proxy), zoom=1.45)
    render(out(4, "on_finger_fingertip_end"), worn, (-1, 0, 0), DORSAL_UP, f"{tag}: on finger, from the FINGERTIP end",
           (size, "Back of the hand up, palm side down. Finger rests in the V saddle; strap passes behind the saddle plate.", proxy), zoom=1.9)
    render(out(5, "on_finger_palm_iso"), worn + [(fov, FOV, 0.16)], (-0.55, 0.75 * L.rs, 0.80), DORSAL_UP,
           f"{tag}: on finger, palm-side three-quarter view",
           (size, "Blue cone = assumed lens direction with Seeed's stated 102 deg diagonal field of view. Camera placement needs physical verification.", proxy),
           parallel=False, zoom=1.15)
    render(out(6, "on_finger_back_iso"), worn, (-0.60, 0.85 * L.rs, -0.65), DORSAL_UP, f"{tag}: on finger, back-of-hand three-quarter view",
           (size, proxy), parallel=False, zoom=1.15)
    render(out(7, "holder_only_print_orientation"), solo, (-0.70, 0.90 * L.rs, 0.60), (0, 0, 1), f"{tag}: printed part only, as it sits on the print bed",
           (size, "Cap face down on the bed. Fingertip end toward the viewer."), zoom=0.95)
    render(out(8, "holder_only_saddle_side"), solo, (0.45, 1.0 * L.fs, 0.45), (0, 0, 1), f"{tag}: printed part only, finger (saddle) side",
           (size, "V saddle with the strap slot behind it. The wall on this side is solid: no contacts face the skin."), zoom=0.95)
    export_context(name, parts)


def flip_to_dorsal_up(shape, L: LayoutV2, y_shift):
    """Rotate a V2 shape 180 deg about the finger axis direction so the back of the hand is up, finger centre at (y_shift, 0)."""
    s = shape.rotate(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), 180)
    # After the flip the finger centre is at (-y_fc, -z_fc).
    return s.translate(cq.Vector(0, y_shift + L.y_fc, L.z_fc))


def render_comparison():
    """V1 backup, V2A and V2B side by side from the fingertip end, fingers on one line."""
    items = []
    # V1: finger under the holder.
    p1 = Params()
    L1 = Layout(p1)
    zc1 = ref.finger_centre_z(L1)
    x1 = ref.load_xiao(L1)
    v1 = [
        (build_holder(p1).val(), HOLDER), (x1["board"], BOARD), (ref.ov3660(L1, x1["camera_cad"]), CAMERA),
        (ref.header_strips(L1), HEADER), (ref.duponts(L1), JUMPER), (ref.usb_plug(L1), PLUG), (ref.finger(L1), SKIN),
    ]
    # Seen from the fingertip end with the back of the hand up, image-right is -Y.
    for shape, color in v1:
        items.append((shape.translate(cq.Vector(0, 50, -zc1)), color, 1.0))
    for name, y_shift in (("ReTrace_Ring_V2A_current_hardware_noIMU", 0.0), ("ReTrace_Ring_V2B_low_profile_harness_CONCEPT", -62.0)):
        p = VARIANTS[name][0]
        L, parts, _ = parts_for(p)
        for n, (shape, color) in parts.items():
            if n in ("REF_strap", "REF_gate_pin_filament"):
                continue
            items.append((flip_to_dorsal_up(shape, L, y_shift), color, 1.0))
    render(VIEWS / "compare_V1_V2A_V2B_fingertip_end.png", items, (-1, 0, 0), (0, 0, 1),
           "Fingertip-end view, left to right: V1 backup, V2A current hardware, V2B harness concept",
           ("Back of the hand up, palm side down. V1 lens points up from the back of the finger; V2 lens points down at the palm-side edge.",
            "All fingers are the same ASSUMED 19 mm cylinder. Board = Seeed STEP; other parts are unmeasured proxies. IMU not shown for V1 / V2A."), zoom=1.5)


def main():
    VIEWS.mkdir(parents=True, exist_ok=True)
    for name, (p, desc) in VARIANTS.items():
        render_variant(name, p, desc)
    render_comparison()


if __name__ == "__main__":
    main()
