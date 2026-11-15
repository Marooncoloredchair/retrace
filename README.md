# ReTrace

ReTrace walks you back to something you put down. The phone in your pocket measures the walk. A ring on your finger buzzes faster as you get closer. A laptop keeps the path and drives the ring.

This is the system we are taking to Hack Princeton.

## How a session works

1. Put the phone in a pocket and start a Sensor Logger recording.
2. Stand at the object and press Save.
3. Walk away.
4. Press Find. The ring pulses slowly when you are far and quickly when you are close. Under a meter it buzzes rapidly.

The phone page for that flow is `/app` on the laptop. Add it to the home screen if you want it full screen.

## Architecture

- **Phone.** Sensor Logger HTTP-pushes orientation, gyroscope, pedometer, compass, and acceleration. Heading comes from the orientation quaternion, so a phone standing up in a pocket still has a direction. A step moves the dot only while a walking rhythm is present. Turning in place counts a step and does not move the dot.
- **Ring.** A Seeed XIAO ESP32S3 Sense. The firmware in `firmware/xiao_sense_imu_logger/` is what we flash for the pocket demo: it logs motion and pulses the motor when the laptop sends `v` over USB. The camera firmware in `firmware/xiao_sense_usb_motor/` is the other build, for place-find from pictures.
- **Laptop.** `.\retrace.cmd` starts one Python process. It owns the ring link, the pocket path, place find, and the dashboard at `http://127.0.0.1:8010/`.

The laptop prints two URLs at startup: the Sensor Logger push URL, and the phone page.

## Hardware

- Seeed Studio XIAO ESP32S3 Sense, worn as the ring, with a vibration motor
- A phone running [Sensor Logger](https://github.com/tszheichoi/awesome-sensor-logger), HTTP Push on, Standardise Units & Frames off
- A laptop on the same Wi-Fi as the phone, with the ring on a data cable when the buzz has to come from the laptop

## Run

```powershell
cd $env:USERPROFILE\Projects\retrace
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\retrace.cmd
```

Leave that window open. On the phone, start the recording, then open the phone page URL from the laptop window.

`ring_config.json` is local and gitignored. The default link is USB serial. A Wi-Fi firmware build reads `firmware/xiao_sense_usb_motor/wifi_config.h`, also gitignored. Copy `wifi_config.example.h` to that name and fill it in on your machine.

The logger sketch needs `firmware/xiao_sense_imu_logger/guide_cert.h` for its on-ring page. That file holds a local certificate and key and is not in this repo. `guide_page.h` is the page itself and is included.

Naming a put-down from a picture reads `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` from the environment. Neither key is stored here.

Stop the service with Ctrl+C.

## What we measured

Plots in [docs/](docs/). Recordings and camera frames stay on the machine that recorded them.

- [body_replay_heading.png](docs/body_replay_heading.png) — pocket walks replayed from the phone quaternion. A turn that the phone holds comes back toward the start.
- [body_replay_frozen.png](docs/body_replay_frozen.png) — one walk whose heading never reversed, replayed with the pocket offset frozen. A constant offset only rotates that line.
- [walk_01.png](docs/walk_01.png) — gyro yaw through a segmented walk, with the turning stretches marked.
- [find_heat_mark_2.png](docs/find_heat_mark_2.png) — place-find heat around one put-down, from the ring camera.

On the still-turn recording, after the walking-rhythm gate, the dot moved 0 m. On the back-and-forth recording, the return passed within 0.02 m of the outbound path.

## Known limitations

A short out-and-back folds back on itself. A long leg can still drift. On that same back-and-forth recording, the path ended 6.1 m from the start after the return had already come back to the outbound line.

## What stays off GitHub

`recordings/`, `reports/`, `body_sessions/`, the memory database, `ring_config.json`, Wi-Fi credentials, certificates, and the dashboard token. Those files are gitignored.
