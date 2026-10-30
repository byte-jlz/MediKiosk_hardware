#!/usr/bin/env python3
"""
edge_service.py - the ML + SQLite part of the kiosk. Runs next to kiosk_agent.py on the Pi.

    kiosk_agent.py --medikiosk/K-01/result--> [local Mosquitto] --> edge_service.py
                                                                       |  1. save every scan result in SQLite
                                                                       |  2. when the visit has all 4 steps OK:
                                                                       |     classify (ML) + queue for the backend
                                                                       v
                                         medikiosk/K-01/edge/assessment   (progress only - NEVER the tier)
    sync_worker.py reads the SQLite outbox and sends it to the backend (separate program)

It never touches the sensors, and the agent needs no changes. The tier is staff-only, so it is stored
and sent to the backend, but it is NOT published on the local broker the kiosk screen listens to.

Run:  python3 edge_service.py [edge_config.json]
"""
import json, os, queue, signal, sys, threading, time, traceback
from pathlib import Path

import paho.mqtt.client as mqtt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_db
from visit_recorder import VisitRecorder

NEEDED = ("bmi", "respirationSpo2Hr", "bloodPressure", "temperature")     # the four kiosk steps


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} [edge] {msg}", flush=True)


def to_results(scans):
    """The agent's `values` dicts -> the dict visit_recorder expects."""
    bmi = scans["bmi"]
    return {"finger": scans["respirationSpo2Hr"], "bp": scans["bloodPressure"], "temp": scans["temperature"],
            "height": {"height_cm": bmi.get("height_cm")}, "weight": {"weight_kg": bmi.get("weight_kg")}}


class EdgeService:
    def __init__(self, cfg):
        base = Path(cfg.get("base_dir", "."))
        cfg["base_dir"] = str(base if base.is_absolute() else (HERE / base).resolve())
        Path(cfg["db_path"]).parent.mkdir(parents=True, exist_ok=True)
        self.cfg, self.kiosk = cfg, cfg["kiosk_id"]
        self.t_result = f"medikiosk/{self.kiosk}/result"
        self.t_out = f"medikiosk/{self.kiosk}/edge/assessment"
        self.t_status = f"medikiosk/{self.kiosk}/edge/status"
        self.recorder = VisitRecorder(cfg)                    # loads the ML model once
        self.conn = edge_db.connect(cfg["db_path"])           # used by the worker thread only
        self.inbox = queue.Queue()
        self.stopping = threading.Event()
        b = cfg["local_broker"]
        self.b = b
        # fixed client id + clean_session=False: results published while we restart are delivered afterwards
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.kiosk}-edge", clean_session=False)
        if b.get("username"):
            self.client.username_pw_set(b["username"], b.get("password"))
        self.client.will_set(self.t_status, json.dumps({"online": False}), qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.on_connect = self._on_connect
        self.client.on_message = lambda c, u, msg: self.inbox.put(msg.payload)

    def _on_connect(self, client, _u, _f, rc, _p):
        if rc.is_failure:
            log(f"MQTT connect failed: {rc}"); return
        client.subscribe(self.t_result, qos=1)
        client.publish(self.t_status, json.dumps({"online": True, "model": self.cfg["model_version"]}), qos=1, retain=True)
        log(f"connected to {self.b['host']}:{self.b['port']} - listening on {self.t_result}")

    def say(self, payload):
        self.client.publish(self.t_out, json.dumps(payload), qos=1)

    # ---- one agent result message ----
    def handle(self, raw):
        try:
            r = json.loads(raw)
        except ValueError:
            log("ignored a result that is not JSON"); return
        vital, ok, scan_id = r.get("vital"), bool(r.get("ok")), r.get("id")
        if vital not in NEEDED or not scan_id:
            return                                            # single tests (height, weight...) are not part of a visit
        session = r.get("session") or "no-session"
        if edge_db.session_completed(self.conn, session):
            log(f"scan {vital} for finished session {session} ignored"); return
        if not edge_db.save_scan(self.conn, str(scan_id), session, vital, ok, json.dumps(r)):
            return                                            # duplicate delivery
        if not ok:
            log(f"{session}: {vital} failed ({r.get('error')}) - waiting for a retry"); return
        have = edge_db.latest_ok_scans(self.conn, session)
        missing = [v for v in NEEDED if v not in have]
        if missing:
            log(f"{session}: {vital} saved, waiting for {missing}"); return
        out = self.recorder.record(to_results(have), external_session_id=session)
        if out["status"] == "saved":
            log(f"{session}: classified and queued as {out['message_id'][:8]} "
                f"(tier {out['tier']}, in model range: {out['in_model_range']}, flags: {out['critical_flags'] or 'none'})")
            self.say({"session": session, "status": "saved", "message_id": out["message_id"],
                      "in_model_range": out["in_model_range"], "critical": bool(out["critical_flags"]), "ts": round(time.time(), 3)})
        else:
            log(f"{session}: {out['status']} - redo {out['redo_vitals']}")
            self.say({"session": session, "status": out["status"], "redo_vitals": out["redo_vitals"], "ts": round(time.time(), 3)})

    def run(self):
        self.client.connect_async(self.b["host"], self.b["port"], keepalive=30)
        self.client.loop_start()
        while not self.stopping.is_set():
            try:
                raw = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.handle(raw)
            except Exception:
                traceback.print_exc()                         # a bug must not stop the service
        self.client.publish(self.t_status, json.dumps({"online": False}), qos=1, retain=True).wait_for_publish(timeout=2)
        self.client.loop_stop(); self.client.disconnect()


def main(cfg_path=None):
    cfg_path = cfg_path or str(HERE / "edge_config.json")
    svc = EdgeService(json.load(open(cfg_path)))
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: svc.stopping.set())
    svc.run()


if __name__ == "__main__":
    main(*sys.argv[1:])
