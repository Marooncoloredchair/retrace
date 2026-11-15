# ReTrace Ring V2 - lab note

First-fit prototype. Dimensions come from Seeed's published CAD and from nominal part sizes.
**Nothing was measured on the actual hardware**, so treat the first print as a fit test.

## What changed from V1

V1 put the board on the back of the finger with the lens pointing up. V2 turns the whole
stacked board over and carries it **beside** the finger, so the lens sits at the palm-side
edge of the finger and points away from the palm side. The header pins now point toward the
back of the hand and are closed off by a cap.

## Files

| File | Use |
| --- | --- |
| `ReTrace_Ring_V2A_current_hardware.step / .stl` | **Print this one.** Fits the hardware as it is now (header pins + Dupont jumpers). Includes the MPU6050 shelf. |
| `ReTrace_Ring_V2A_current_hardware_noIMU.step / .stl` | Same, without the shelf (17 mm shorter). |
| `ReTrace_Ring_V2B_low_profile_harness_CONCEPT.step / .stl` | Concept only. 10.5 mm thinner, but the Dupont jumpers do **not** fit in it. Needs the soldered harness in `ReTrace_Ring_V2_wiring_harness_options.md`. |
| `*_dimensions.md` | Dimension sheet per holder, every parameter tagged with its source. |
| `views/` | PNG views on a finger: top, side, palm side, fingertip end, two three-quarter views. |
| `reference_only/*.step` | Assembly with finger, board, camera, pins, plug. For viewing, not for printing. |
| `../V1_backup/` | The previous design, unchanged, kept as a backup. |
| `../../retrace_ring_v2.py` | Editable CadQuery source. Parameters are in `ParamsV2` and the inherited `Params`. |

## Size

| | V1 backup | V2A current hardware | V2B harness concept |
| --- | --- | --- | --- |
| Holder, along finger x sideways x thickness | 57 x 22 x 35 | 57 x 31 x 36 (40 long without shelf) | 40 x 31 x 26 |
| Above the back of the finger | about 42 (lens tip) | 16.5 | 6.0 |
| Below the palm-side skin | 0 | 0.7 | 0.7 |
| Sideways from the finger skin, toward the thumb | 1.5 each side | 27 | 27 |

V2A is not a smaller part than V1. It moves the bulk from on top of the finger to beside it and
puts the lens where it was wanted. The 20.5 mm of pins and jumpers under the board is what sets
its thickness; only a different wiring harness (V2B) reduces that.

## Printing

- Orientation: as exported, cap face flat on the bed, Z up.
- Designed to print without supports (45-degree webs under the saddle and nose; short bridges
  at the camera rest, saddle underside, jumper window and guard bar). It has not been sliced.
- PETG or PLA, 0.2 mm layers, 3 or more perimeters. The pin post and guard posts are 1.6 mm thick.
- Left hand: set `finger_side=1` in `retrace_ring_v2.py` and re-export. The exported files are for a right hand.

## Assembly (V2A)

1. Holder with the open face up. Hold the camera up off the board.
2. Slide the board in from the fingertip end, USB-C first, jumpers already plugged on. The
   jumpers run in the two channels; wires leave through the side window or the open channel end.
3. Push a 10 mm piece of 1.75 mm filament into the vertical hole at the fingertip end (stops the board sliding out).
4. Retaining stub: push 1.75 mm filament into the small hole in the low post on the thumb side, at
   the fingertip end. It enters the gap between the two stacked boards and sits over the bare corner
   of the main board. Stop with 3.5 to 4 mm inside, bend the outside end over and tape it to the post.
   Do not put anything across the top of the board: the camera connector and ribbon are there.
5. Stick the camera to the camera rest with about 1.6 mm of foam tape. The ribbon should lie flat with no fold.
6. Plug in USB-C and cable-tie the cable to the nose.
7. Foam-tape the MPU6050 to the shelf; a cable tie through the two notches holds it.
8. Thread the strap through the slot behind the V saddle and around the finger.

## Check on the first print

| Check | Designed value |
| --- | --- |
| Board drops between the walls with slight play | pocket 18.78, board 17.78 |
| Jumpers fit under the board without pressing on the cap | 20.5 from board underside to cap |
| USB-C plug seats fully between the two stop posts | 14.4 opening, plug assumed 13.4 or less |
| Retaining stub enters the gap between the boards and touches only the bare board corner | hole 0.4 above the main board; 2.9 gap between boards per Seeed CAD |
| Camera sits on the rest with the ribbon relaxed | rest is 6.6 above the board underside |
| Both buttons reachable | from the open face, beside the camera; expect to need a pen tip |
| Lens is not shadowed by the guard walls | walls 0.7 beyond the estimated lens tip |
| Strap passes through the slot | 13.7 x 3.0 |
| Finger sits in the V without the tips digging in | V sized for a 19 mm finger |

## Not verified

Orientation:

- That "beside the finger, lens at the palm-side edge" is the intended placement. This is my reading of the request.
- Right hand, holder on the thumb side of the index finger.
- That a 27 mm wide part between index finger and thumb is acceptable in use. Not something CAD can answer; try it with the first print or a cardboard block of the same size.
- Lens direction and position. Taken from where Seeed's CAD places its OV2640; the OV3660 on this board was not measured.

Dimensions (all nominal or assumed):

- Finger diameter 19 mm.
- Depth of header pins plus Dupont jumpers plus wire bend: 20.5 mm.
- USB-C plug overmold size.
- Camera height above the board (lens tip estimated 13.5 mm above the board underside), and whether a microSD card is fitted under it.
- Whether a heat sink is fitted under the board (a 7 mm pocket is provided).
- GY-521 board size (about 21 x 16) and which way its header pins point.
- Strap thickness.

The CAD interference checks (`check_fit_v2.py`) pass against Seeed's model and these nominal
proxies. That shows the model is self-consistent. It does not show the print will fit the real parts.

## Known compromises

- The board's component side is open toward the palm (needed for the lens and buttons). Nothing
  conductive faces the finger the holder is strapped to, but the thumb or an object held in the
  hand can touch the board. Kapton tape over the exposed board, leaving the lens and buttons clear, fixes that.
- When the finger curls, the fingertip and thumb will appear in the camera image.
- Board retention is untested. The stub holds one corner at the fingertip end; at the wrist end the
  board can lift 0.5 mm before the USB-C shell meets the camera rest. An earlier revision used a pin
  across the board 0.4 mm above the camera connector; it was removed because that gap was a CAD value.
- The folder `../../SEND_TO_URI/` holds the V2A files and lab note prepared for the print lab.
