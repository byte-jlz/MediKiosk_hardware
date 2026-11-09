#!/usr/bin/env python3
"""Staff side of the EMQX test: shows every classified visit the kiosks upload, and acks it
(the ack is what makes the kiosk mark the visit 'synced').

    python staff_receiver.py

Broker address comes from edge/sync.env (MEDIKIOSK_BROKER_HOST / PORT / TLS), the same file the
kiosk uses. The staff login is separate, also in sync.env:

    MEDIKIOSK_STAFF_USERNAME=backend
    MEDIKIOSK_STAFF_PASSWORD=...

Kiosk side (other terminal):  python try_vitals.py --emqx
"""
import json, os, ssl, sys, time
from pathlib import Path
import paho.mqtt.client as mqtt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from try_vitals import load_env_file

COLOR = {"High": "\033[41;97m", "Medium": "\033[43;30m", "Low": "\033[46;30m", "Normal": "\033[42;30m"}
RESET = "\033[0m"
seen = set()


def show(d):
    v, b = d.get("vitals", {}), d.get("body", {})
    tier = d.get("urgency", "?")
    print(f"\n{time.strftime('%H:%M:%S')}  {COLOR.get(tier, '')} {tier.upper()} {RESET}  "
          f"kiosk {d.get('kiosk_id')}  patient {d.get('patient_ref')}  visit {d.get('message_id', '')[:8]}")
    print(f"  SpO2 {v.get('spo2')}%   BP {v.get('systolic')}/{v.get('diastolic')}   HR {v.get('heart_rate')}   "
          f"Temp {v.get('temperature')} C   RR {v.get('respiratory_rate')}   BMI {b.get('bmi')}")
    probs = d.get("probabilities") or {}
    print("  model: " + "  ".join(f"{k} {p * 100:.0f}%" for k, p in sorted(probs.items(), key=lambda kv: -kv[1])))
    if d.get("critical_flags"):
        print("  CRITICAL: " + ", ".join(d["critical_flags"]))
    if not d.get("in_model_range", True):
        print("  note: some values are outside the model's training range")


def on_message(client, userdata, msg):
    try:
        d = json.loads(msg.payload)
        mid = d["message_id"]
    except (ValueError, KeyError):
        return
    dup = mid in seen
    if not dup:
        seen.add(mid); show(d)
    client.publish(f"medikiosk/{d['kiosk_id']}/ack",
                   json.dumps({"message_id": mid, "status": "duplicate" if dup else "stored"}), qos=1)


def on_connect(client, userdata, flags, rc, props):
    if rc.is_failure:
        print(f"broker refused the connection: {rc} (check MEDIKIOSK_STAFF_USERNAME / PASSWORD)"); return
    client.subscribe("medikiosk/+/vitals/#", qos=1)
    print(f"staff connected to {host}:{port} - waiting for visits on medikiosk/+/vitals/# (Ctrl+C to stop)")


load_env_file(HERE / "sync.env")
env = os.environ.get
host = env("MEDIKIOSK_BROKER_HOST") or sys.exit("set MEDIKIOSK_BROKER_HOST in edge/sync.env")
port = int(env("MEDIKIOSK_BROKER_PORT") or 8883)
tls_env = env("MEDIKIOSK_BROKER_TLS")
tls = tls_env.strip().lower() in ("1", "true", "yes", "on") if tls_env is not None else port == 8883

os.system("")                                   # turns on ANSI colours in the Windows console
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="staff-receiver", clean_session=False)
if env("MEDIKIOSK_STAFF_USERNAME"):
    c.username_pw_set(env("MEDIKIOSK_STAFF_USERNAME"), env("MEDIKIOSK_STAFF_PASSWORD"))
if tls:
    c.tls_set(ca_certs=env("MEDIKIOSK_BROKER_CA_FILE") or None, tls_version=ssl.PROTOCOL_TLS_CLIENT)
c.on_connect, c.on_message = on_connect, on_message
print(f"connecting to {host}:{port}{' (TLS)' if tls else ''}...")
c.connect(host, port, keepalive=30)
try:
    c.loop_forever()
except KeyboardInterrupt:
    c.disconnect()
