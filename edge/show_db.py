#!/usr/bin/env python3
"""Look inside the kiosk database (read-only).   python3 show_db.py [path/to/medikiosk.db] [how_many_visits]"""
import json, sqlite3, sys

path = sys.argv[1] if len(sys.argv) > 1 else json.load(open("edge_config.json"))["db_path"]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
try:
    RANGE = json.load(open("edge_config.json"))["training_range"]
except Exception:
    RANGE = {}
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

print("Outbox (what the backend will receive):")
for r in c.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status"):
    print(f"  {r['status']:8} {r['n']}")
print(f"\nLatest {n} visits:\n")
for a in c.execute("""SELECT a.assessment_id, a.tier, a.captured_at, s.external_id, o.status, o.priority, o.topic, o.payload
                      FROM assessments a JOIN outbox o ON o.message_id=a.assessment_id
                      JOIN sessions s ON s.session_id=a.session_id ORDER BY a.captured_at DESC LIMIT ?""", (n,)):
    p = json.loads(a["payload"])
    v = p["vitals"]
    print(f"{a["captured_at"]}  session {a['external_id']}  ->  {a["tier"].upper()}  (outbox: {a['status']}, priority {a['priority']})")
    print(f"  SpO2 {v['spo2']}%  BP {v['systolic']}/{v['diastolic']}  HR {v['heart_rate']}  Temp {v['temperature']}  RR {v['respiratory_rate']}")
    out = [f"{k} {v[k]} (model saw {lo}-{hi})" for k, (lo, hi) in RANGE.items() if v.get(k) is not None and not lo <= v[k] <= hi]
    if out:
        print(f"  OUTSIDE training range: {', '.join(out)}")
    print(f"  body {p['body']}  critical {p['critical_flags'] or 'none'}  in model range {p['in_model_range']}")
    print(f"  probabilities {p['probabilities']}")
    print(f"  will be sent to topic {a['topic']}  (message {a['assessment_id'][:8]})\n")