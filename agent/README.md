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

### `cmd`: what you can send

```json
{"action": "scan",    "id": "a1b2c3", "vital": "bmi", "session": "S-0001"}
{"action": "confirm", "id": "a1b2c3", "step": "step_on"}
{"action": "cancel",  "id": "a1b2c3"}
{"action": "ping"}
```

* `id` is any unique text you choose. Every `state`/`result` for that scan carries it.
* `session` is optional. Use the same value for all scans of one check-in.
  Use a session ID, **never the patient's name** (privacy).
* `vital`: `bmi`, `height`, `weight`, `heartRateSpo2`, `temperature`,
  `bloodPressure`, `respiration` (the same names as the frontend's `vitalKey`).
  `bmi` = weight (tare, then step on) followed by height, while standing on the scale.
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
| `countdown_end` | countdown finished | |
| `finished` / `failed` / `cancelled` | scan over. `result` follows | `message` |
| `rejected` | a command was refused (busy, unknown vital, ...) | `message` |

Confirm steps: `scale_empty`, `step_on`, `stand_under` (bmi/weight/height),
`finger_on` (heartRateSpo2), `finger_rest` (respiration), `forehead`
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
