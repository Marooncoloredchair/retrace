# ReTrace Ring V2A - lab note

**First-fit prototype, not a verified final enclosure.** The dimensions come from Seeed's
published CAD of the XIAO ESP32S3 Sense and from nominal part sizes. Nothing has been measured
on the actual hardware yet. The hardware will be brought in for fit checks.

## In this folder

| File | Use |
| --- | --- |
| `ReTrace_Ring_V2A_current_hardware.step` / `.stl` | The part to print. One solid, millimetres. |
| `ReTrace_Ring_V2A_dimension_sheet.md` | Every dimension with its source (Seeed CAD, assumed, design choice). |
| `V2A_view_1_top.png`, `V2A_view_2_side.png`, `V2A_view_3_palm_side.png` | Labelled views of the holder on a finger. |

## Verify before printing

| # | Check on the hardware | The design assumes | If it differs |
| --- | --- | --- | --- |
| 1 | Which hand | Right index finger, holder on the thumb side | The part must be mirrored |
| 2 | Depth under the main board: board underside to the far end of the Dupont jumpers, including the wire bend | 20.5 mm or less | Deepen the holder by the difference |
| 3 | Camera: where it lies with its ribbon relaxed, and the height of the lens tip above the main board underside | Over the USB-C end of the board, lens tip at 13.5 mm. **Not verified** (see below) | Camera rest and guard height change |
| 4 | USB-C plug: width of the moulded body | 13.4 mm or less (opening is 14.4) | Widen the opening |
| 5 | Finger diameter where the ring sits | 19 mm. Other sizes still strap in, but the lens moves relative to the palm-side surface of the finger | Saddle position changes |
| 6 | MPU6050 board size | About 21 x 16 mm (shelf is 17 x 22) | Lengthen the shelf |

Every one of these is a single parameter in the CadQuery source. Send the measured numbers back
and a corrected file can be exported in a few minutes.

## Camera placement needs physical verification

The camera in the views is drawn where Seeed's CAD puts an older camera (OV2640). This board has
an OV3660 on a very short ribbon, and its real position, height and lens direction have not been
measured. Do not treat the drawn camera as correct.

What the holder provides is a flat rest above the USB-C shell (6.6 mm above the main board
underside) for the camera to be foam-taped to, and guard walls 0.7 mm beyond the assumed lens
tip. If the camera cannot lie on the rest with the ribbon relaxed and unfolded, do not force it.

## Printing

- Orientation: as exported. The closed cap face is flat on the bed, Z up.
- Intended to print without supports (45-degree webs, short bridges). It has not been sliced; if
  the slicer shows a problem area, use build-plate-only supports.
- PLA or PETG, 0.2 mm layers, 3 or more perimeters. Thinnest walls are 1.6 mm.
- Size 57 x 31 x 36 mm, about 12.3 cm3.

## Assembly

1. Set the holder with the open face up. Hold the camera up off the board.
2. Slide the board in from the fingertip end, USB-C first, with the Dupont jumpers already plugged
   onto the header pins. The jumpers run in the two channels under the board. Wires leave through
   the side window or the open ends of the channels.
3. Gate pin: push a 10 mm piece of 1.75 mm filament into the vertical hole in the centre block at
   the fingertip end. It stops the board sliding back out.
4. Retaining stub: push 1.75 mm filament into the small hole in the low post on the thumb side, at
   the fingertip end. It enters the gap **between** the two stacked boards and sits over the bare
   corner of the main board. Stop with 3.5 to 4 mm inside, measured from the outer face of the
   post. Bend the outside end over and tape it to the post.
5. Foam-tape the camera to the rest (about 1.6 mm of foam). The ribbon should lie flat.
6. Plug in USB-C and cable-tie the cable to the nose.
7. Foam-tape the MPU6050 to the shelf. A cable tie through the two notches holds it.
8. Thread the strap through the slot behind the V saddle and around the finger.

**Do not put a pin, tie or clip across the top of the board.** The camera connector and ribbon are there.

## Fit checks with the hardware

| Check | Designed value |
| --- | --- |
| Board drops between the walls with slight play | Pocket 18.8, board 17.8 |
| Jumpers fit under the board without pressing on the cap | 20.5 from board underside to cap |
| USB-C plug seats fully | 14.4 opening |
| Retaining stub enters the gap between the boards and touches nothing but the bare board corner | Hole is 0.4 above the main board; 2.9 gap between boards per Seeed CAD |
| Antenna lead, if fitted, is not under the stub | Stub ends about 1 mm short of the antenna connector per Seeed CAD |
| Camera lies on the rest with the ribbon relaxed | Rest 6.6 above the board underside |
| Lens is not shadowed by the guard walls | Walls 0.7 beyond the assumed lens tip |
| Both buttons can be pressed | From the open face, beside the camera; expect to need a pen tip |
| Strap passes through the slot | 13.7 x 3.0 |
| Thumb has room beside the holder | Holder stands 27 mm out from the finger |

## Known risks

- **Camera placement** is unverified, as above.
- **Board retention is untested.** The stub holds one corner at the fingertip end. At the wrist end
  the board can lift 0.5 mm before the USB-C shell meets the camera rest; the cable tie on the nose
  also holds that end. An earlier revision used a pin across the board 0.4 mm above the camera
  connector. That was removed because the gap was a CAD value and a small error would put the pin
  on the connector.
- **27 mm beside the finger**, between the index finger and thumb. Whether that is comfortable can
  only be judged with the print on a hand.
- **The component side of the board is open toward the palm**, for the lens and buttons. Nothing
  conductive faces the finger the holder is strapped to. Kapton tape over the exposed board, clear
  of the lens and buttons, stops the thumb or a held object touching it.
- The CAD interference checks pass against Seeed's model and nominal stand-ins for the jumpers, plug,
  camera, MPU6050 and finger. That shows the model is self-consistent. It does not show the print
  will fit the real parts.
