#!/usr/bin/env python3
"""Try the urgency model on your laptop with vitals YOU type in. No sensors, no Pi, no broker.

It uses the same Classifier, model file and limits as edge_service.py, so the tier you see
here is the tier the kiosk would assign for the same numbers.

    python try_vitals.py                         # interactive: change vitals, see the tier
    python try_vitals.py spo2=88 temp=39.4       # one-shot: classify and exit
    python try_vitals.py --emqx                  # act as kiosk K-01: 'send' uploads to EMQX
                                                 # (login from edge/sync.env; staff side: staff_receiver.py)

Interactive commands:
    spo2 88   sys 150   dia 95   hr 110   temp 38.5   rr 24   height 170   weight 65
    (several at once is fine:  spo2 88 hr 120 temp 39)
    normal    reset to a healthy baseline
    save      also store this visit in test_edge.db (classified + queued in the outbox,
              like a real visit; see it with: python show_db.py test_edge.db)
    send      (--emqx only) classify, queue and upload this visit; waits for the staff ack
    q         quit
"""
import json, os, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from classifier import Classifier, ReadingRejected

ALIASES = {"spo2": "spo2", "o2": "spo2", "sys": "systolic", "systolic": "systolic",
           "dia": "diastolic", "diastolic": "diastolic", "hr": "heart_rate", "heart_rate": "heart_rate",
           "temp": "temperature", "temperature": "temperature", "rr": "respiratory_rate",
           "respiratory_rate": "respiratory_rate", "height": "height_cm", "height_cm": "height_cm",
           "weight": "weight_kg", "weight_kg": "weight_kg"}
BASELINE = {"spo2": 98, "systolic": 118, "diastolic": 76, "heart_rate": 72, "temperature": 36.6,
            "respiratory_rate": 16, "height_cm": 167, "weight_kg": 62.4}
UNITS = {"spo2": "%", "systolic": "mmHg", "diastolic": "mmHg", "heart_rate": "bpm",
         "temperature": "C", "respiratory_rate": "/min", "height_cm": "cm", "weight_kg": "kg"}


def load_cfg():
    for name in ("edge_config.json", "edge_config.example.json"):
        p = HERE / name
        if p.exists():
            cfg = json.load(open(p))
            cfg["base_dir"] = str(HERE)        # model files live next to this script
            return cfg
    sys.exit("no edge_config.json or edge_config.example.json in " + str(HERE))


def parse(tokens, vitals):
    """Accepts 'spo2 88 hr 120' or 'spo2=88 hr=120'. Returns an error string or None."""
    tokens = [t for tok in tokens for t in tok.replace("=", " ").split()]
    if len(tokens) % 2:
        return "give each vital a value, e.g.  spo2 88"
    for name, val in zip(tokens[::2], tokens[1::2]):
        key = ALIASES.get(name.lower())
        if not key:
            return f"unknown vital '{name}' (use: {', '.join(sorted(set(ALIASES)))})"
        try:
            f = float(val)
            vitals[key] = int(f) if f.is_integer() else f
        except ValueError:
            return f"'{val}' is not a number"
    return None


def show(clf, vitals):
    print()
    for k, v in vitals.items():
        print(f"  {k:<17}{v:g} {UNITS[k]}")
    bmi = vitals["weight_kg"] / (vitals["height_cm"] / 100) ** 2
    print(f"  {'bmi':<17}{bmi:.1f}   (shown only; not a model input)")
    try:
        r = clf.classify(vitals)
    except ReadingRejected as e:
        print(f"\n  REJECTED: {e}  -> outside hard_limits, the kiosk would ask to measure again")
        return None
    print(f"\n  URGENCY: {r['tier'].upper()}  (rank {r['tier_rank']} of {len(clf.classes) - 1})")
    for tier, p in sorted(r["probabilities"].items(), key=lambda kv: -kv[1]):
        print(f"    {tier:<7}{p * 100:5.1f}%  {'#' * round(p * 30)}")
    if r["critical_flags"]:
        print("  critical flags: " + ", ".join(r["critical_flags"]))
    if not r["in_model_range"]:
        print("  note: outside the training range for " + ", ".join(r["outside_range"])
              + " (the model is guessing beyond what it was trained on)")
    return r


def save(cfg, vitals, result):
    import edge_db
    conn = edge_db.connect(str(HERE / "test_edge.db"))
    sid = edge_db.start_session(conn, patient_ref="P-TEST")
    mid = edge_db.save_assessment(conn, cfg["kiosk_id"], sid, vitals, result, cfg.get("model_version", "manual"))
    print(f"  saved visit {mid[:8]} to test_edge.db (outbox now: {edge_db.queue_depth(conn)})")


def load_env_file(path):
    """KEY=VALUE lines (like /etc/medikiosk/sync.env) into os.environ. Real env vars win."""
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


DEMO_DB = HERE / "emqx_demo.db"      # its own database, so old test visits are not uploaded too


def start_uplink(cfg):
    """Runs the real sync_worker.py in a background thread against DEMO_DB."""
    if not load_env_file(HERE / "sync.env") and not os.environ.get("MEDIKIOSK_BROKER_HOST"):
        sys.exit("no edge/sync.env - copy edge/sync.env.example to edge/sync.env and fill in the EMQX login")
    import sync_worker
    cfg = dict(cfg, db_path=str(DEMO_DB), ack_timeout_s=10)
    ready = threading.Event()

    def run():                        # SyncWorker opens SQLite in __init__, so build it in this thread
        w = sync_worker.SyncWorker(cfg)
        ready.w = w; ready.set()
        w.run()
    threading.Thread(target=run, daemon=True).start()
    ready.wait()
    return ready.w


def send(cfg, vitals, result, worker):
    import edge_db
    conn = edge_db.connect(str(DEMO_DB))
    sid = edge_db.start_session(conn, patient_ref="P-TEST", external_id=f"SES-DEMO-{int(time.time())}")
    mid = edge_db.save_assessment(conn, cfg["kiosk_id"], sid, vitals, result, cfg.get("model_version", "manual"))
    print(f"  queued visit {mid[:8]} ({result['tier']}) - uploading...")
    if not worker.connected.is_set():
        print("  (not connected to EMQX yet - it is kept and sent once connected)")
    for _ in range(150):              # up to 15 s
        row = conn.execute("SELECT status FROM outbox WHERE message_id=?", (mid,)).fetchone()
        if row["status"] == "synced":
            print("  delivered: staff acknowledged it"); return
        time.sleep(0.1)
    print("  no ack from staff yet (is staff_receiver.py running?) - it stays queued and is retried")


def main():
    cfg = load_cfg()
    clf = Classifier(cfg)
    vitals = dict(BASELINE)
    args = sys.argv[1:]
    worker = None
    if "--emqx" in args:
        args.remove("--emqx")
        worker = start_uplink(cfg)
        print(f"kiosk {cfg['kiosk_id']} - uplink to {worker.broker['host']}:{worker.broker['port']}")
    if args:                                  # one-shot mode
        err = parse(args, vitals)
        if err: sys.exit(err)
        result = show(clf, vitals)
        if worker and result:
            send(cfg, vitals, result, worker)
        return
    print(__doc__.split("Interactive commands:")[1])
    result = show(clf, vitals)
    while True:
        try:
            line = input("\nvitals> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not line: continue
        cmd = line.lower()
        if cmd in ("q", "quit", "exit"): break
        if cmd == "normal":
            vitals = dict(BASELINE)
        elif cmd == "save":
            if result: save(cfg, vitals, result)
            else: print("  nothing to save: the current reading is rejected")
            continue
        elif cmd == "send":
            if not worker: print("  start with --emqx to send"); continue
            if result: send(cfg, vitals, result, worker)
            else: print("  nothing to send: the current reading is rejected")
            continue
        else:
            err = parse([line], vitals)
            if err: print("  " + err); continue
        result = show(clf, vitals)


if __name__ == "__main__":
    main()
