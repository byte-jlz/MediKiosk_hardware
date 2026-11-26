# edge/ - ML classifier + SQLite buffer + uplink

Sits beside `agent/` and `hardware/`. **`kiosk_agent.py` and `measurements.py` are unchanged.**

```
kiosk_agent.py --medikiosk/K-01/result--> local Mosquitto --> edge_service.py --> SQLite (scans, readings, assessments, outbox)
                                                                                      |
                                                       sync_worker.py <---------------+   --> backend broker (acks required)
```

| File | Job |
|---|---|
| `edge_service.py` | listens to the agent's `result` messages, stores every scan, and when bmi + respirationSpo2Hr + bloodPressure + temperature are all OK for one session: classifies and queues it. **Guest scans (no session) are ignored**: not saved, classified or sent - only logged-in patients get an ML assessment |
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

## Connect to EMQX Cloud (the backend broker)
1. In the EMQX console create a **Serverless** deployment (set the spend limit to 0 = free quota only).
2. Access Control -> Authentication: add one login per kiosk (e.g. `K-01`) and one for the backend (`backend`).
3. Put this kiosk's login in `/etc/medikiosk/sync.env`, **not** in `edge_config.json` (the repo is public):
   ```
   sudo mkdir -p /etc/medikiosk
   sudo cp edge/sync.env.example /etc/medikiosk/sync.env
   sudo nano /etc/medikiosk/sync.env        # address from EMQX Overview, port 8883, K-01 + password
   sudo chown root:medi_kiosk /etc/medikiosk/sync.env && sudo chmod 640 /etc/medikiosk/sync.env
   ```
   The `MEDIKIOSK_BROKER_*` variables override `broker` in `edge_config.json`. TLS turns on by itself on
   port 8883, and EMQX Cloud needs no CA file.
4. Re-copy the service file (it now reads that env file), then start the worker:
   ```
   sudo cp edge/medikiosk-sync.service /etc/systemd/system/ && sudo systemctl daemon-reload
   sudo systemctl enable --now medikiosk-sync
   journalctl -u medikiosk-sync -f       # expect: "connected to xxxx.emqxsl.com:8883"
   ```
   `broker refused the connection: Not authorized` = wrong username/password.
   `could not reach ...` = wrong address/port or no internet (visits wait in SQLite meanwhile).

`local_broker` stays the Pi's own Mosquitto - only `sync_worker.py` talks to EMQX.

## Tests (on any computer with mosquitto installed)
```
python3 edge/selftest.py            # 20 checks: classifier, QR, offline buffer, order, outage, crash, duplicates
python3 edge/test_visit_recorder.py # 14 checks
```

## Triage rules and field-test logging (new)

All triage numbers live in **one file, `triage_config.json`** (status: PLACEHOLDER until the health center / medical validator signs off).
`triage_rules.py` reads it; nothing else holds a threshold.

| Setting | Current value | Meaning |
|---|---|---|
| `gate` | 0.80 | below this top probability a result gets a "Low model confidence" flag (design choice, not a medical standard). Display only |
| `tie_break` | `"probability"` | order *inside* a tier for the staff queue: `"probability"` (default: higher probability of the patient's tier first) or `"wait"` (longest waiting first). The outbox always sends highest tier first, then oldest first |
| `max_bypass` | `2` | bounded waiting for the `"probability"` rule: a patient can be passed by at most this many newer patients of the same tier, then goes next, so nobody is skipped indefinitely. Design choice; the clinic can change it |
| `prob_step` | `0.0` | `0` compares probabilities exactly (strict). Set e.g. `0.05` to group probabilities into 5-point bands so near-equal patients fall to waiting time. Only used when `tie_break` is `"probability"` |
| `critical_first` | `false` | `false`: the critical-value flag is display only (default). `true`: inside a tier, a patient with a critical value goes first, then the probability and waiting-time rules. The tier still decides first. Turn on only if the validator agrees; the order then depends on `critical_limits`, which are placeholders |
| `critical_limits` | NEWS2-based placeholders | display flag only by default. Never changes the tier. Changes the order inside a tier only if `critical_first` is true. Critical vital + Normal/Low tier adds a "check" flag |

**Flow per visit** (`visit_recorder.py`): classify -> save -> send. The tier alone sets the transmission priority and the queue order.
A low-confidence result carries `alert_level: "LOW_CONFIDENCE"` and a critical vital with a Normal/Low tier carries `check: true`; both are
display flags for the staff dashboard. There is no retake and no staff-review priority. The patient screen is told only "saved / redo", never the tier.

**Where the logging lives:** at the edge, in the same SQLite file, written by `visit_recorder.record`:

| Table | One row per | Holds |
|---|---|---|
| `field_log` | classification attempt | vitals, quality flags, full probabilities, tier, confidence, alert level (OK / LOW_CONFIDENCE), check flag, critical flags, in-training-range flag, model version, tie-break setting |
| `staff_feedback` | staff decision | the staff member's own urgency tier for that visit (the field-test ground truth), override flag, seen-at time, note |
| `queue_snapshots` | queue update | the queue in both orders (probability and wait) and whether they differ |

No names or contact details are logged here.

**Recording the staff decision** (shadow mode: staff triage as usual, the kiosk is advisory). Staff write their own tier on the paper form
(`field_test_staff_form.csv` is the template), then someone enters it:

    python field_log.py feedback <db> <visit id, first 8 characters> <High|Medium|Low|Normal> --staff AB --note "..."

**After the test:**

    python field_log.py summary <db>
    python field_log.py export  <db> <folder>   # field_log.csv, staff_feedback.csv, queue_snapshots.csv, summary.txt

The summary reports visits, low-model-confidence results, check flags, critical-vital results, kiosk-vs-staff agreement among unflagged and low-model-confidence results, kiosk-vs-staff agreement, under- and over-triage,
staff-High missed by the kiosk, and how often the two tie-break orders disagreed (the evidence for the validator's choice).

**Deployment notes:** the Pi must run `scikit-learn==1.8.0` (the joblib model was trained with it). Eligibility (room air, alert,
no oxygen therapy) is screened by staff **before** the kiosk. The staff dashboard is outside this repo: it must read `alert_level`, `confidence` and `check`
from the payload (display only) and order the queue with `triage_rules.queue_order`. Do not ship `sync.env` or the `*.db` files.

## Still to decide / do
- Critical-value thresholds in the config are placeholders - replace with a cited guideline.
- Session start/finish still goes over HTTP in the current kiosk site; that fails offline.
- `TEMP_OFFSET_C = 2.9` in measurements.py came from one reading - validate.
- Model was trained with scikit-learn 1.8.0; if loading fails on the Pi, retrain with the Pi's versions.
- Local Mosquitto: bind to 127.0.0.1 and add a password before deployment.
