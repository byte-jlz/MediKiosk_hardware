# MediKiosk agent (MQTT)

`kiosk_agent.py` is the **only** program that talks to the kiosk sensors.
The kiosk tablet and the backend never touch the hardware. They send it
commands over MQTT and listen for what it reports. Only one measurement
runs at a time, and the live feed pauses during it, so nothing else ever
shares I2C bus 1 with a measurement.

```
                       medikiosk/K-01/cmd
 kiosk tablet / tests  ------------------->  kiosk_agent.py  --->  hardware/measurements.py
 (later: backend)      <-------------------        |                (all 6 vitals)
                       state / result / live       |
                       status (retained)           +--> ESP32 scale (USB, kept open)
```

## Run it

```bash
pip install -r requirements.txt       # includes paho-mqtt

# Laptop, no sensors: fake hardware, short scans
SIMULATE=1 SIM_FAST=1 python agent/kiosk_agent.py
#   PowerShell:  $env:SIMULATE="1"; $env:SIM_FAST="1"; python agent\kiosk_agent.py

# On the Pi, real sensors
python agent/kiosk_agent.py
```

You need a broker running first (Mosquitto, with the config in the frontend
repo: `backend/mosquitto/medikiosk.conf`, which has port 1883 for Python and
9001 for browsers).

Try it without the frontend, from a second terminal:

```bash
python agent/try_agent.py bmi            # press Enter at each "ready?" step
python agent/try_agent.py all --auto     # every vital, auto-confirmed
python agent/try_agent.py --watch        # print everything the agent publishes
python agent/try_agent.py --cancel
```

Settings are environment variables. They're listed at the top of `kiosk_agent.py`
(`KIOSK_ID`, `MQTT_HOST`, `MQTT_USER`, `CONFIRM_TIMEOUT`, `SENSOR_MOUNT_MM`, ...).
Sensor tuning still lives in `hardware/measurements.py`.

### SIMULATE=1

`sim_hardware.py` swaps in fake sensor **libraries** (smbus, max30102,
VL53L0X, pyserial, bp_auto). The real `measurements.py` code still runs,
with the same countdowns, confirm steps and RR signal analysis. Every new
`session` gets a new random pretend patient. `SIM_FAST=1` shortens the scans
(HR 6 s, RR 24 s, BP 8 s), so a full check-in takes about 1 minute.

## Topics

Everything is under `medikiosk/<KIOSK_ID>/` (default `K-01`). Every JSON
message has `ts` (unix seconds).

| Topic | Who publishes | QoS | Retained | What |
|---|---|---|---|---|
| `status` | agent | 1 | yes | online/busy info. Also the **last-will**, so a crash or power loss shows `"online": false` |
| `cmd` | tablet / tests | 1 | no | commands to the agent |
| `state` | agent | 0 for `measuring`, else 1 | no | progress of the current scan |
| `result` | agent | 1 | no | final outcome of every scan, **always sent**, even on cancel or failure |
| `live/tof`, `live/temp` | agent | 0 | no | readings every 0.5 s while idle |
| `live/tof/status`, `live/temp/status` | agent | 1 | yes | `online` / `paused` / `offline` |
| `qr` | agent | 1 | no | QR login progress + the code (see below). The code is **never** sent on `result` |
| `qr/preview` | agent | 0 | no | raw JPEG bytes (480 px wide, ~5/s) of the QR cam, so the patient can aim |
| `system` | agent | 1 | no | answers to staff commands (`shutdown`, `staff_pin`, `dev_end`, see below) |

### `cmd`: what you can send

```json
{"action": "scan",    "id": "a1b2c3", "vital": "bmi", "session": "S-0001", "patient": "P-00001"}
{"action": "confirm", "id": "a1b2c3", "step": "step_on"}
{"action": "cancel",  "id": "a1b2c3"}
{"action": "ping"}
{"action": "qr_start", "id": "q1w2e3"}
{"action": "qr_stop",  "id": "q1w2e3"}
{"action": "shutdown", "id": "s1d2f3", "pin": "1234"}
```

`patient` (optional) is the signed-in patient's id from the kiosk's QR login. The agent copies it
into the `result` message, and `edge/edge_service.py` saves the visit under that patient. Guests send
`session: null` and no `patient` - the edge ignores their scans.

* `id` is any unique text you choose. Every `state`/`result` for that scan carries it.
* `session` is optional. Use the same value for all scans of one check-in.
  Use a session ID, **never the patient's name** (privacy).
* `vital`: the kiosk flow uses `bmi`, `respirationSpo2Hr`, `bloodPressure`,
  `temperature`. `bmi` = weight (tare, then step on) followed by height while
  standing on the scale; `respirationSpo2Hr` = ONE 30 s finger reading that
  gives respiratory rate, SpO2 and heart rate. Single tests: `height`,
  `weight`, `heartRateSpo2` (20 s), `respiration`.
* `step` in `confirm` is optional. If given, it must match the step being waited for.
* `"auto_confirm": true` in a scan skips all confirm steps (for tests only).
* A scan sent while another is running is **rejected** (`phase: "rejected"`).

### `state`: the scan's progress

```json
{"id": "a1b2c3", "vital": "bmi", "session": "S-0001", "phase": "awaiting_confirm",
 "step": "step_on", "message": "Step ONTO the scale", "timeout_s": 120, "ts": 1793165478.9}
```

| phase | meaning | extra fields |
|---|---|---|
| `accepted` | scan started | |
| `awaiting_confirm` | the person must do something, then send `confirm` | `step`, `message`, `timeout_s` |
| `confirmed` | got the confirm | `step` |
| `info` / `warning` | a line to show or log | `message` |
| `measuring` | countdown tick | `message`, `seconds_left`, `live` (e.g. `{"kg": 70.1}`) |
| `measuring` (bloodPressure) | elapsed tick, ~1/s - counts UP, same seconds as the agent log | `message`, `elapsed_s`, `timeout_s`, `bp_phase` (`pumping` / `measuring` / `checking`) |
| `countdown_end` | countdown finished | |
| `finished` / `failed` / `cancelled` | scan over. `result` follows | `message` |
| `rejected` | a command was refused (busy, unknown vital, ...) | `message` |

Confirm steps: `scale_empty`, `step_on`, `stand_under` (bmi/weight/height),
`finger_on` (heartRateSpo2), `finger_rest` (respirationSpo2Hr, respiration), `forehead`
(temperature), `cuff_on` (bloodPressure). If nobody confirms within
`CONFIRM_TIMEOUT` (120 s), the scan is cancelled.

### `result`: the outcome

```json
{"id": "a1b2c3", "vital": "bmi", "session": "S-0001", "kiosk": "K-01",
 "ok": true, "cancelled": false, "error": null,
 "reading": {"value": 24.1, "unit": "kg/m²",
             "weight": {"value": 70.04, "unit": "kg"}, "height": {"value": 170.4, "unit": "cm"}},
 "values": { "...all raw numbers from measurements.py..." },
 "simulated": false, "duration_s": 21.4, "ts": 1793165499.1}
```

`reading` uses the shape the frontend wizard already expects:

| vital | reading |
|---|---|
| bmi | `{value, unit: "kg/m²", weight: {value, unit}, height: {value, unit}}` |
| height / weight | `{value, unit: "cm" / "kg"}` |
| respirationSpo2Hr | `{respiration: {value, unit: "br/min", confidence, low?, high?}, spo2: {value, unit: "%"}, heartRate: {value, unit: "bpm", source: "finger_sensor"}}` |
| heartRateSpo2 | `{heartRate: {value, unit: "bpm"}, spo2: {value, unit: "%"}}` |
| temperature | `{value, unit: "°C"}` |
| bloodPressure | `{systolic, diastolic, pulse, unit: "mmHg"}` |
| respiration | `{value, unit: "br/min", confidence: "CONFIRMED" or "ESTIMATE", low?, high?}` |

When `ok` is false, `reading` is `null` and `error` says why in plain words
(e.g. "no valid reading - finger not on the sensor?").

### `live/tof` and `live/temp`

The same JSON as the old `tof_mqtt_publisher.py` / `temp_mqtt_publisher.py`,
so the existing frontend cards work by pointing `VITE_TOF_TOPIC` /
`VITE_TEMP_TOPIC` at `medikiosk/K-01/live/tof` and `medikiosk/K-01/live/temp`.
The ToF message also has `height_cm` once `SENSOR_MOUNT_MM` is set.

### `qr`: QR login (agent/qr_reader.py)

The QR cam and the BP cam are the same webcam model, so the browser can't
tell them apart. The agent opens the QR cam by its USB port instead
(`QR_CAMERA` in measurements.py = hub port 1.2, bottom blue) and decodes the
code with OpenCV. It runs next to a scan (its own camera), not in the scan queue.

```json
{"id": "q1w2e3", "phase": "started", "camera": 2}
{"id": "q1w2e3", "phase": "found",   "text": "<the QR code's text>"}
{"id": "q1w2e3", "phase": "timeout"}                 // nothing found in QR_TIMEOUT s (120)
{"id": "q1w2e3", "phase": "error",   "error": "QR cam not found at ..."}
```

After `found` / `timeout` / `error` the camera is closed; send `qr_start`
again to look for another code. A new `qr_start` replaces a running one.
SIMULATE=1 uses the laptop's webcam (camera 0) with the real OpenCV.

### `shutdown`: staff turns the Pi off (hidden panel on the kiosk)

On the kiosk's start screen, press and hold the clock for 5 seconds, type the
4-digit PIN and tap "Shut down kiosk". The agent checks the PIN (the kiosk page
never knows it), refuses while a scan is running, and locks for 60 s after 3
wrong PINs in a row. It answers on `system`:

```json
{"id": "s1d2f3", "action": "shutdown", "ok": true,  "message": "shutting down"}
{"id": "s1d2f3", "action": "shutdown", "ok": false, "message": "Wrong PIN."}
```

With SIMULATE=1 (or anywhere that is not the Pi) the PIN is checked but the
computer is NOT turned off (`"message": "simulated"`).

One-time setup on the Pi:

```bash
# 1. the PIN (4 digits, NOT in git) - change 1234 to your own
sudo mkdir -p /etc/medikiosk
echo 1234 | sudo tee /etc/medikiosk/shutdown_pin >/dev/null
sudo chown root:medi_kiosk /etc/medikiosk/shutdown_pin && sudo chmod 640 /etc/medikiosk/shutdown_pin

# 2. let the agent run ONLY "shutdown -h now" without a password
echo 'medi_kiosk ALL=(root) NOPASSWD: /usr/sbin/shutdown -h now' | sudo tee /etc/sudoers.d/medikiosk-shutdown >/dev/null
sudo chmod 440 /etc/sudoers.d/medikiosk-shutdown
sudo visudo -c                       # must say "parsed OK"
```

To change the PIN later, repeat the `tee` line (no restart needed).

### `staff_pin` / `dev_end`: one hidden PIN pad, two PINs

Holding the clock 5 s opens a plain "Staff access" PIN pad. The page sends the
PIN with `staff_pin`, and the agent says what it opens. Nothing on the screen
says there are two PINs.

```json
{"action": "staff_pin", "id": "p1", "pin": "1234"}
{"id": "p1", "action": "staff_pin", "ok": true, "opens": "shutdown"}
{"id": "p1", "action": "staff_pin", "ok": true, "opens": "developer", "token": "<32 hex>"}
{"id": "p1", "action": "staff_pin", "ok": false, "message": "Wrong PIN."}
```

- The shutdown PIN (`/etc/medikiosk/shutdown_pin`) only opens the shutdown
  screen. The `shutdown` command itself still checks it again, and refuses the
  developer PIN.
- The developer PIN (`/etc/medikiosk/dev_pin`, or `DEV_PIN_FILE`) opens
  Developer options. The answer carries a session token: every developer command
  must send it, and the session ends 10 min after the last one, or on
  `{"action": "dev_end", "token": "..."}` when the screen closes.
- Both PINs share the lockout (3 wrong in a row = 60 s).
- The two PINs must be different. If they are the same, the agent refuses both
  and logs why.

One-time setup on the Pi (keep this PIN to the team, don't give it to the health center):

```bash
echo 5678 | sudo tee /etc/medikiosk/dev_pin >/dev/null      # your own 4 digits, NOT the shutdown PIN
sudo chown root:medi_kiosk /etc/medikiosk/dev_pin && sudo chmod 640 /etc/medikiosk/dev_pin
```

Laptop test: put a different PIN in a second file and set
`$env:DEV_PIN_FILE="C:\Users\User\Desktop\devpin.txt"` before starting the agent.

### `dev_*`: Developer options calibration (needs the developer session token)

All of these are in `agent/dev_tools.py`. Every command carries `"token"` from the
`staff_pin` answer and is answered on `system`. Live data for the open screen comes on
`medikiosk/<id>/dev` (JSON) and `medikiosk/<id>/dev/bpcam/preview` (JPEG).

| command | what it does |
|---|---|
| `dev_cal_get` | current `TEMP_OFFSET_C`, `SPO2_OFFSET`, `SENSOR_MOUNT_MM`, `TOF_SCALE` (+ whether each comes from `calibration.json` or the default) |
| `dev_cal_set {values}` | saves into `hardware/calibration.json` and uses it at once (refuses odd values, e.g. a temperature offset outside -2 to +8 °C) |
| `dev_scale {op}` | `watch` (live weight, auto-zero OFF while the screen is open), `stop`, `tare`, `calibrate {kg}` (ESP32 `C:`), `save` (ESP32 `S`), `restore {factor}` (ESP32 `F:`) |
| `dev_bpcam {op}` | `start` (opens the BP camera + light), `keepalive`, `set {settings}` (boxes / sliders), `reset`, `light {on}`, `save` (writes `hardware/bp_cam_settings.json`, old one kept as `bp_cam_settings.backup.json`), `stop` |
| `dev_wifi {op}` | `status` (network, IP, internet, cloud broker reachable, visits waiting to send), `scan`, `connect {ssid, password}` - `agent/wifi_tools.py`, NetworkManager's `nmcli` on the Pi, pretend on the laptop (password `wrongpass` always fails there) |
| `dev_shutdown` | turn the Pi off (no second PIN - the developer PIN already let you in) |
| `dev_system {op}` | `restart`, `desktop {stop}`, `terminal` - runs `kiosk_control.sh` (repo root) DETACHED, because restart / `desktop --stop` also stop this agent. Log: `~/medikiosk-logs/control.log`. Pretend on the laptop. |

- Temperature uses the live temperature feed: `live/temp` now also sends `raw_c` (before the offset).
- SpO2 and height use normal scans (`respirationSpo2Hr`, `height`) with no session, so nothing is saved for a patient.
- Wi-Fi: the password goes to `nmcli` once; it is never written to the agent log or sent back. If connecting
  fails, a half-made profile is deleted and NetworkManager goes back to the saved network. If the Pi says
  "not authorized", the kiosk user is missing from the `netdev` group: `sudo usermod -aG netdev medi_kiosk`, reboot.
- Leaving the kiosk (Exit to desktop / Open terminal) puts a **MediKiosk** icon on the desktop and in the
  app menu; tapping it runs `kiosk_control.sh back` (starts the programs if they're stopped, then the kiosk page).
  The desktop on-screen keyboard is squeekboard (`sudo apt install squeekboard` if it is missing).
- The terminal is a normal shell as the kiosk user (with its sudo rights) - one more reason to keep the
  developer PIN to the team.
- The scale factor is kept in the ESP32's flash (survives power off), not in `calibration.json`.
- Closing Developer options (`dev_end`) or a timed-out session stops the camera and the live weight.
- On the Pi, a calibration saved here changes `hardware/calibration.json` / `bp_cam_settings.json`
  on the Pi only. To keep it in git, copy it back to the laptop and commit it.

## Known limits (for now)

* **BP can't be cancelled mid-measurement.** `bp_auto.measure()` runs for
  40–70 s without reporting back, so a cancel only takes effect when it
  finishes. It also still prints its own `[info]` lines to the log.
* The broker allows anonymous clients (`medikiosk.conf`). That's fine on the
  closed testing network. Add a password file + ACLs before the kiosk goes on a
  shared network or the internet (`MQTT_USER` / `MQTT_PASSWORD` are already
  supported here).

## On the Pi: start on boot (do this at the end)

`medikiosk-agent.service` runs the agent with systemd and restarts it if it
crashes. Check the paths inside match your Pi, then:

```bash
sudo cp agent/medikiosk-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now medikiosk-agent
journalctl -u medikiosk-agent -f        # watch its log
```
