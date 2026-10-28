#!/usr/bin/env python3
"""
try_agent.py - poke the kiosk agent from a terminal (no frontend needed).

    python agent/try_agent.py bmi                 # scan; press Enter at each "ready?" step
    python agent/try_agent.py temperature --auto  # scan; confirm steps automatically
    python agent/try_agent.py all --auto          # every vital, one after another
    python agent/try_agent.py --watch             # just print everything the agent says
    python agent/try_agent.py --cancel            # cancel whatever is running
    python agent/try_agent.py --ping              # show the agent's status

Vitals: bmi, respirationSpo2Hr, bloodPressure, temperature (the kiosk flow)
        also: height, weight, heartRateSpo2, respiration (single tests)
Uses the same MQTT_HOST / MQTT_PORT / KIOSK_ID / MQTT_USER / MQTT_PASSWORD
environment variables as kiosk_agent.py.
"""

import argparse
import json
import os
import queue
import sys
import time
import uuid

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("pip install paho-mqtt")

KIOSK_ID = os.getenv("KIOSK_ID", "K-01")
BASE = f"medikiosk/{KIOSK_ID}"
ALL = ["bmi", "respirationSpo2Hr", "bloodPressure", "temperature"]   # the kiosk flow


def connect():
    events = queue.Queue()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"try-agent-{uuid.uuid4().hex[:6]}")
    if os.getenv("MQTT_USER"):
        c.username_pw_set(os.getenv("MQTT_USER"), os.getenv("MQTT_PASSWORD"))

    def on_connect(client, *_):
        client.subscribe([(f"{BASE}/status", 1), (f"{BASE}/state", 1), (f"{BASE}/result", 1)])
        events.put(("connected", None))

    def on_message(_c, _u, msg):
        try:
            events.put((msg.topic.rsplit("/", 1)[-1], json.loads(msg.payload)))
        except ValueError:
            events.put((msg.topic.rsplit("/", 1)[-1], msg.payload.decode(errors="ignore")))

    c.on_connect, c.on_message = on_connect, on_message
    c.connect(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")), 30)
    c.loop_start()
    kind, _ = events.get(timeout=10)
    return c, events


def send(c, cmd):
    c.publish(f"{BASE}/cmd", json.dumps(cmd), qos=1)


def scan(c, events, vital, auto, session):
    job_id = uuid.uuid4().hex[:8]
    send(c, {"action": "scan", "id": job_id, "vital": vital, "session": session})
    print(f"\n>>> scan {vital} (id {job_id})")
    counting = False
    while True:
        kind, ev = events.get()
        if not isinstance(ev, dict) or ev.get("id") not in (job_id, None):
            continue
        if kind == "status":
            continue
        if kind == "result":
            if counting:
                print()
            print("<<< RESULT", json.dumps({k: ev[k] for k in ("ok", "error", "reading")}, ensure_ascii=False))
            return ev
        phase = ev.get("phase")
        if phase == "measuring":
            print(f"\r    {ev.get('message', '')}  {ev.get('seconds_left', 0):4.1f}s left ", end="", flush=True)
            counting = True
            continue
        if counting:
            print()
            counting = False
        if phase == "countdown_end":
            continue
        if phase == "awaiting_confirm":
            print(f"    [{ev['step']}] {ev['message']}")
            if not auto:
                input("    press Enter when ready...")
            send(c, {"action": "confirm", "id": job_id, "step": ev["step"]})
            continue
        if phase == "rejected":
            print(f"<<< REJECTED: {ev.get('message')}")
            return ev
        print(f"    {phase}: {ev.get('message', '')}")


def main():
    ap = argparse.ArgumentParser(description="Send commands to the MediKiosk agent")
    ap.add_argument("vital", nargs="?", help="vital to scan, or 'all'")
    ap.add_argument("--auto", action="store_true", help="confirm every step automatically")
    ap.add_argument("--watch", action="store_true", help="print everything the agent publishes")
    ap.add_argument("--cancel", action="store_true", help="cancel the running scan")
    ap.add_argument("--ping", action="store_true", help="print the agent status")
    args = ap.parse_args()

    c, events = connect()
    if args.cancel:
        send(c, {"action": "cancel"})
        time.sleep(1)
    elif args.ping:
        send(c, {"action": "ping"})
        t_end = time.time() + 3
        while time.time() < t_end:
            try:
                kind, ev = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "status":
                print(json.dumps(ev, indent=2))
                break
    elif args.watch:
        print(f"watching {BASE}/... (Ctrl+C to stop)")
        try:
            while True:
                kind, ev = events.get()
                print(kind, json.dumps(ev, ensure_ascii=False) if isinstance(ev, dict) else ev)
        except KeyboardInterrupt:
            pass
    elif args.vital:
        session = f"try-{uuid.uuid4().hex[:6]}"
        for vital in (ALL if args.vital == "all" else [args.vital]):
            scan(c, events, vital, args.auto, session)
    else:
        ap.print_help()
    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
