"""
save_readings.py - a BACKUP COPY of every reading the kiosk takes.

Runs next to the agent and writes down every result the agent publishes,
straight to files on the Pi. It does not depend on the edge service, the
internet, EMQX or the staff dashboard - if those fail, the readings are
still here.

    cd ~/Desktop/MediKiosk_hardware && source venv/bin/activate
    python3 agent/save_readings.py

Files (one pair per day, in ~/medikiosk-backup/ - outside the repo, never in git):
    readings-2026-11-11.csv     one row per reading - opens in Excel / Google Sheets
    results-2026-11-11.jsonl    the agent's full message for every result (nothing left out)

What is saved:
    every result: registered patients AND guests, successful AND failed readings
    (failed ones have ok = no and the error, so you can see how often a sensor failed)
    --no-guests     leave out guest readings (session = none)

Every line is written to the SD card immediately (flush + fsync), so a power cut
loses at most the reading that was being written.
If this program is restarted, the broker hands over the results it missed while it
was away (persistent MQTT session) - start it once and leave it running.

Privacy: these files contain health readings and patient IDs. Keep them on the Pi,
and delete or hand them over according to your consent form / the clinic's rules.

Settings (same as the agent):  KIOSK_ID (K-01)  MQTT_HOST (localhost)  MQTT_PORT (1883)
                               MQTT_USER / MQTT_PASSWORD     BACKUP_DIR (~/medikiosk-backup)
"""

import argparse
import csv
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

KIOSK_ID = os.getenv("KIOSK_ID", "K-01")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(Path.home() / "medikiosk-backup"))).expanduser()
T_RESULT = f"medikiosk/{KIOSK_ID}/result"

STEP_NAME = {"bmi": "BMI", "respirationSpo2Hr": "Breathing & oxygen", "bloodPressure": "Blood pressure",
             "temperature": "Temperature", "height": "Height", "weight": "Weight",
             "heartRateSpo2": "Heart rate & SpO2", "respiration": "Breathing rate"}

COLUMNS = ["date", "time", "kiosk", "step", "ok", "error", "patient_id", "guest", "session",
           "bmi", "weight_kg", "height_cm",
           "spo2_pct", "heart_rate_bpm", "heart_rate_source", "respiration_bpm", "respiration_confidence",
           "systolic_mmhg", "diastolic_mmhg", "bp_pulse_bpm",
           "temperature_c",
           "duration_s", "scan_id", "simulated"]


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} [backup] {msg}", flush=True)


def _v(d, *keys):
    """d['a']['b'] without crashing on missing parts."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def to_row(r):
    """One agent result -> one CSV row (the values the patient saw on the screen)."""
    t = datetime.fromtimestamp(r.get("ts") or time.time())
    rd = r.get("reading") or {}
    vital = r.get("vital")
    row = {
        "date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M:%S"), "kiosk": r.get("kiosk", KIOSK_ID),
        "step": STEP_NAME.get(vital, vital), "ok": "yes" if r.get("ok") else "no", "error": r.get("error") or "",
        "patient_id": r.get("patient") or "", "guest": "yes" if not r.get("session") else "no",
        "session": r.get("session") or "", "duration_s": round(r.get("duration_s") or 0, 1),
        "scan_id": r.get("id", ""), "simulated": "yes" if r.get("simulated") else "",
    }
    if vital == "bmi":
        row.update(bmi=rd.get("value"), weight_kg=_v(rd, "weight", "value"), height_cm=_v(rd, "height", "value"))
    elif vital == "weight":
        row.update(weight_kg=rd.get("value"))
    elif vital == "height":
        row.update(height_cm=rd.get("value"))
    elif vital in ("respirationSpo2Hr", "heartRateSpo2", "respiration"):
        row.update(spo2_pct=_v(rd, "spo2", "value"), heart_rate_bpm=_v(rd, "heartRate", "value"),
                   heart_rate_source=_v(rd, "heartRate", "source"),
                   respiration_bpm=_v(rd, "respiration", "value") if vital != "respiration" else rd.get("value"),
                   respiration_confidence=_v(rd, "respiration", "confidence") if vital != "respiration" else rd.get("confidence"))
    elif vital == "bloodPressure":
        row.update(systolic_mmhg=rd.get("systolic"), diastolic_mmhg=rd.get("diastolic"), bp_pulse_bpm=rd.get("pulse"))
    elif vital == "temperature":
        row.update(temperature_c=rd.get("value"))
    return {k: ("" if row.get(k) is None else row.get(k)) for k in COLUMNS}


class Backup:
    def __init__(self, folder, include_guests=True):
        self.folder, self.include_guests = folder, include_guests
        self.lock = threading.Lock()
        self.count = 0
        folder.mkdir(parents=True, exist_ok=True)

    def _append(self, path, write):
        new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            write(f, new)
            f.flush()
            os.fsync(f.fileno())                       # really on the SD card, not just in memory

    def save(self, r):
        if not self.include_guests and not r.get("session"):
            return
        day = datetime.fromtimestamp(r.get("ts") or time.time()).strftime("%Y-%m-%d")
        row = to_row(r)
        with self.lock:
            self._append(self.folder / f"results-{day}.jsonl",
                         lambda f, new: f.write(json.dumps(r, ensure_ascii=False) + "\n"))

            def write_csv(f, new):
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                if new:
                    w.writeheader()
                w.writerow(row)
            self._append(self.folder / f"readings-{day}.csv", write_csv)
            self.count += 1
        who = row["patient_id"] or ("guest" if row["guest"] == "yes" else "-")
        what = row["error"] if row["ok"] == "no" else ", ".join(
            f"{k}={row[k]}" for k in COLUMNS[9:21] if row[k] != "")
        log(f"saved #{self.count}: {row['step']} ({who}) {'OK' if row['ok'] == 'yes' else 'FAILED'} - {what}")


def main():
    ap = argparse.ArgumentParser(description="Save a backup copy of every kiosk reading to files on the Pi.")
    ap.add_argument("--no-guests", action="store_true", help="don't save guest readings (no session)")
    ap.add_argument("--dir", default=str(BACKUP_DIR), help=f"where to save (default {BACKUP_DIR})")
    args = ap.parse_args()

    backup = Backup(Path(args.dir).expanduser(), include_guests=not args.no_guests)
    # fixed client id + clean_session=False: results published while this program was stopped are
    # kept by the broker and delivered when it starts again
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"medikiosk-backup-{KIOSK_ID}", clean_session=False)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.reconnect_delay_set(1, 10)

    def on_connect(c, _u, _f, rc, _p):
        if rc.is_failure:
            log(f"MQTT connect failed: {rc}")
            return
        c.subscribe(T_RESULT, qos=1)
        log(f"connected to {MQTT_HOST}:{MQTT_PORT} - saving every result from {T_RESULT}")
        log(f"files: {backup.folder}/readings-<date>.csv  +  results-<date>.jsonl"
            + ("  (guests left out)" if not backup.include_guests else ""))

    def on_message(_c, _u, msg):
        try:
            r = json.loads(msg.payload)
            if isinstance(r, dict):
                backup.save(r)
        except Exception as e:                         # a bad message or a full disk must not stop the backup
            log(f"could not save a result ({e!r}) - raw message: {msg.payload[:300]!r}")
            try:
                with open(backup.folder / "unsaved-raw.log", "ab") as f:
                    f.write(msg.payload + b"\n")
            except OSError:
                pass

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = lambda *a: log("MQTT disconnected - retrying (missed results are delivered on reconnect)")
    stopping = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stopping.set())
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    try:
        while not stopping.wait(0.5):
            pass
    finally:
        client.loop_stop()
        client.disconnect()
        log(f"stopped - {backup.count} reading(s) saved this run")


if __name__ == "__main__":
    sys.exit(main())
