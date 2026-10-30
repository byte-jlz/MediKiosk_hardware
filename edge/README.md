# edge/ - ML classifier + SQLite buffer + uplink

Sits beside `agent/` and `hardware/`. **`kiosk_agent.py` and `measurements.py` are unchanged.**

```
kiosk_agent.py --medikiosk/K-01/result--> local Mosquitto --> edge_service.py --> SQLite (scans, readings, assessments, outbox)
                                                                                      |
                                                       sync_worker.py <---------------+   --> backend broker (acks required)
```

| File | Job |
|---|---|
| `edge_service.py` | listens to the agent's `result` messages, stores every scan, and when bmi + respirationSpo2Hr + bloodPressure + temperature are all OK for one session: classifies and queues it |
| `visit_recorder.py` | agent values -> model inputs (HR = BP pulse, finger as fallback; RR only if CONFIRMED/ESTIMATE) |
| `classifier.py`, `features.py`, `medikiosk_model.joblib`, `medikiosk_config.json` | the model (logistic regression + squared terms) |
| `edge_db.py`, `schema.sql` | SQLite (WAL, synchronous=FULL, outbox written in the same transaction as the result) |
| `sync_worker.py` | sends outbox rows, highest urgency first then oldest, marks synced only on ack |
| `qr_login.py` | verifies signed, single-use QR tokens (not yet wired to the login screen) |
| `show_db.py` | read-only viewer of what is saved and waiting |
| `fake_backend.py` | stand-in backend for testing |

The urgency tier is stored and sent to the backend only. It is **not** published on the local broker
the kiosk screen listens to. The screen only gets `medikiosk/K-01/edge/assessment` (saved / redo, no tier).

## Install on the Pi
```
cd ~/MediKiosk_hardware
venv/bin/pip install -r edge/requirements-edge.txt
cp edge/edge_config.example.json edge/edge_config.json     # then edit it (see below)
mkdir -p ~/medikiosk-data
```
Edit `edge_config.json`: `kiosk_id`, `db_path`, and `broker` (the backend broker; leave localhost until it exists).
`local_broker` is the Mosquitto already running on the Pi.

Test by hand (two terminals, plus the agent in simulate mode):
```
SIMULATE=1 SIM_FAST=1 venv/bin/python agent/kiosk_agent.py
venv/bin/python edge/edge_service.py edge/edge_config.json
venv/bin/python agent/try_agent.py all --auto
```
See what was saved (read-only, safe while the services run):
```
cd edge && ../venv/bin/python show_db.py
```
**Before the backend exists:** enable only `medikiosk-edge`, NOT `medikiosk-sync`. Visits are classified and stay `pending` in the outbox, ready to send. When the backend is up, set `broker` in `edge_config.json` and enable `medikiosk-sync`; everything pending is sent, highest urgency first.

Install as services:
```
sudo cp edge/medikiosk-edge.service edge/medikiosk-sync.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now medikiosk-edge
# later, when the backend exists:  sudo systemctl enable --now medikiosk-sync
journalctl -u medikiosk-edge -f
```

## Tests (on any computer with mosquitto installed)
```
python3 edge/selftest.py            # 20 checks: classifier, QR, offline buffer, order, outage, crash, duplicates
python3 edge/test_visit_recorder.py # 14 checks
```

## Still to decide / do
- Critical-value thresholds in the config are placeholders - replace with a cited guideline.
- Session start/finish still goes over HTTP in the current kiosk site; that fails offline.
- `TEMP_OFFSET_C = 2.9` in measurements.py came from one reading - validate.
- Model was trained with scikit-learn 1.8.0; if loading fails on the Pi, retrain with the Pi's versions.
- Local Mosquitto: bind to 127.0.0.1 and add a password before deployment.
