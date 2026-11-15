# Wiring harness options - can shorter soldered wires replace the Dupont jumpers?

Yes, and it is the only change that makes the holder meaningfully thinner. **None of this has
been done to the hardware**; the printable V2A holder assumes the current Dupont jumpers.

The holder thickness (back of hand to palm) is `under_pcb_clearance + 15.7` mm: 1.5 cap, the
space under the board, 13.5 for the board stack plus camera, 0.7 lens guard. Only the space
under the board can change.

All heights below are nominal figures for standard 2.54 mm parts, not measurements of this board.

| Option | Space needed under the board | Holder thickness | Changes the XIAO? | Reversible? |
| --- | --- | --- | --- | --- |
| 1. Dupont jumpers as now | about 20.5 | 36 (V2A) | No | - |
| 2. Plug-on harness: 1x7 female socket strips with wires soldered to their tails | about 13 | about 29 | No | Yes, unplug it |
| 3. Wires soldered to the side of the existing pins, heat-shrink on each | about 10 | 26 (V2B as exported) | Solder added to the pins only | Yes, desolder the wires; Dupont jumpers fit again |
| 4. Pins trimmed to about 3 mm, then wires soldered | about 7 | about 23 | Pins cut | No |
| 5. Header strips removed, wires soldered to the board pads | about 3 | about 19 | Headers desoldered | No |

Recommendation: option 3 if the lab is comfortable soldering next to the board, otherwise
option 2. Option 2 is built entirely off the board and cannot damage it. Options 4 and 5 are
not worth the risk two days before the event.

To build a holder for any option, set `under_pcb_clearance` in `retrace_ring_v2.py` to the
**measured** height of the finished harness plus 1 mm, and re-export. Measure from the board
underside to the furthest point of the wiring.

## What the harness has to carry

From the firmware in `firmware/`:

| Signal | XIAO pin | Goes to |
| --- | --- | --- |
| 3V3 | 3V3 | MPU6050 VCC |
| GND | GND | MPU6050 GND, motor driver GND |
| SDA | D4 | MPU6050 SDA |
| SCL | D5 | MPU6050 SCL |
| Motor control | D3 | Vibration driver input |
| Motor supply | as currently wired | Vibration driver VCC |

Check the motor supply pin against the existing wiring before copying it.

## Build notes for the lab

- Wire: 28 to 30 AWG stranded, silicone insulation. Keep the MPU6050 leads as short as the
  routing allows; the vibration driver lead as long as it needs to be to reach the wrist.
- Insulation: 1.5 mm heat-shrink over every joint, covering the pin down to the plastic strip.
  No bare metal left on any pin that carries a wire.
- Soldering to the pins (option 3): keep each joint quick. The same pins are soldered into the
  board a few millimetres away, and prolonged heat will soften those joints.
- Routing: the MPU6050 wires leave by the open channel end at the fingertip end. The vibration
  driver wires leave at the wrist end and are cable-tied to the nose with the USB cable, so a
  tug on the lead is not carried by the solder joints.
- MPU6050 in the V2B concept: foam-taped flat to the outside of the cap, which keeps it rigidly
  on the camera assembly. Its own header pins then stand up from the back of the hand; a
  shelf version (`imu_shelf=True`) is the alternative if that is in the way.
- Test continuity and check for shorts between adjacent pins before powering.

## What stays unknown until it is built

- The real height of the finished harness. The 10 mm in V2B is an estimate.
- Whether seven wires bend into the 4 mm wide channels without strain.
- The actual length of the header pins on this board.
