# ReTrace Ring V1 - note for the URI engineering lab

A finger-worn cradle for a Seeed XIAO ESP32S3 Sense (camera board stack with
header pins and jumper wires underneath). Needed for HackPrinceton, which
starts November 13.

**Status: first-fit prototype.** The board geometry comes from Seeed's
manufacturer STEP. Nothing has been measured on the physical hardware yet, so
the dimensions marked ASSUMED in `ReTrace_Ring_V1_dimensions.md` are nominal.
Expect one round of adjustment.

## Files

| File | Use |
| --- | --- |
| `ReTrace_Ring_V1.step` / `.stl` | **Print this.** Holder with a shelf for the motion sensor. 57.0 x 22.0 x 35.1 mm. |
| `ReTrace_Ring_V1_noIMU.step` / `.stl` | Same holder without the shelf, 40.0 x 22.0 x 35.1 mm. Needs no supports. Print as well if there is time. |
| `ReTrace_Ring_V1_dimensions.md` | Every dimension, with its source (CAD, assumed, design choice). |
| `views/*.png` | Top, side, underside, finger-mounted and assembled views. |
| `reference_only/*_REFERENCE_ONLY.step` | Holder with the board and proxy parts in place, for review. **Do not print.** |
| `../../retrace_ring_v1.py` | Editable CadQuery source. All dimensions are in `Params` at the top. |

Units are millimetres. Each printable file is a single solid; the STL is watertight.

## Print orientation

Print it **as exported: Z up, the grooved underside on the bed.** Do not lay
it on its side; the centre pedestal would then need support inside the two
narrow jumper channels, which is hard to remove.

| Setting | Suggested |
| --- | --- |
| Process | FDM, 0.4 mm nozzle, 0.2 mm layers |
| Material | PETG preferred (skin contact, less brittle). PLA is fine for a fit check. |
| Walls | 3 perimeters. Side walls are 1.6 mm, so they print solid. |
| Infill | 20 % or more |
| Supports | **Build plate only.** Needed under the last 9 mm of the sensor shelf (flat underside 8 mm above the bed). The `noIMU` version needs none. |
| Brim | Optional; the footprint on the bed is 26.5 x 22 mm under a 35 mm tall part. |

Everything else is designed to print without support:

- Strap slot: 13.7 mm bridge.
- Finger groove on the underside: 45 degree flanks and an 8 mm bridge. Remove any support the slicer puts there.
- Side windows: 15 mm bridges in a 1.6 mm wall.
- Strain-relief nose: 45 degree underside.
- Camera rest at the top: two bridges of about 16 mm between 45 degree haunches, 1.6 mm thick, with 0.5 mm of air above the USB connector. Slight sag is acceptable. If your slicer bridges this badly, add a support enforcer there; it is open from above and easy to clear.

## Fit checks after printing

Measure these before handing it over. All are loose fits; nothing is a press fit.

| # | Check | Designed | Acceptable |
| --- | --- | --- | --- |
| 1 | Pocket width between the side walls (board is 17.78 wide) | 18.78 | 18.3 or more |
| 2 | Width of each jumper channel beside the centre pedestal (connectors are 2.54) | 4.14 | 3.3 or more |
| 3 | Strap slot, clear through from side to side (strap is 12.7 wide) | 13.7 x 3.0 | 13.0 x 2.5 or more |
| 4 | Gap between the two wrist-end posts (USB plug passes here) | 14.4 | 13.5 or more |
| 5 | Height from the pedestal top pads to the underside of the camera rest (USB connector is 4.46 tall) | 4.96 | 4.7 or more |
| 6 | Gate pin hole in the pedestal top | 2.1 dia | accepts a stub of 1.75 mm filament |
| 7 | Cable-tie slots through the nose | 3.5 x 1.6 | a small cable tie passes |

If the board is available, the real test is:

1. Slide the board in **from the fingertip end** (the end with the shelf), USB connector first, with the header pins hanging in the two channels. It should slide on the pedestal until the board corners meet the wrist-end posts. The USB connector passes under the camera rest.
2. Push a stub of filament into the gate hole behind the board so it cannot slide back out. The stub must stand no more than 3 mm proud.
3. Plug in the USB-C cable. It must seat fully without touching the posts, the camera rest or the nose.
4. Both buttons beside the USB connector must be reachable from above with a fingertip or pen.

If the board will not slide in, the likely causes are the jumper connectors being
longer than assumed (they would hit the floor or the shelf lip) or a heat sink
under the board being taller than the 7 mm relief. Both are single parameters.

## Changing dimensions

```
cd hardware\ring_holder
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe retrace_ring_v1.py     # rebuild STEP, STL, dimension sheet
.venv\Scripts\python.exe check_fit.py           # interference checks against Seeed's STEP
.venv\Scripts\python.exe render_views.py        # PNG views
```

Edit the values in `Params` at the top of `retrace_ring_v1.py`. The ones most likely to change:

- `under_pcb_clearance` (20.5): room under the board for header pins and jumper connectors. This sets the height.
- `clr_side` (0.5): board side clearance.
- `usb_plug_w_max` (13.4), `plug_rest_drop` (2.5): room for the USB plug.
- `strap_slot_h` (3.0), `saddle_w` (12), `saddle_depth` (2): strap and finger fit.
- `imu_shelf` (True/False), `imu_len` (17): motion-sensor shelf.

The STEP files open directly in SolidWorks, Fusion, Onshape or FreeCAD if you prefer to edit there.
