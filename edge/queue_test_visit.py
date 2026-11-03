#!/usr/bin/env python3
"""Put a FAKE classified visit in a test outbox, to test sync_worker.py -> broker -> backend
without the sensors, the kiosk agent or edge_service.py. Safe: it uses its own test database.

    python queue_test_visit.py                 # 1 visit for test patient P-TEST
    python queue_test_visit.py SES-ABC123      # link it to a real kiosk session id from the backend

It also writes edge_config.test.json (same as edge_config.example.json, but pointing at the
test database), so you can then run:

    python sync_worker.py edge_config.test.json
    python show_db.py test_edge.db
"""
import json, random, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edge_db

DB = HERE / "test_edge.db"
CFG = HERE / "edge_config.test.json"

cfg = json.load(open(HERE / "edge_config.example.json"))
cfg.update(db_path=str(DB), ack_timeout_s=10)
json.dump(cfg, open(CFG, "w"), indent=2)

session_ref = sys.argv[1] if len(sys.argv) > 1 else f"SES-TEST-{random.randint(1000, 9999)}"
conn = edge_db.connect(str(DB))
sid = edge_db.start_session(conn, patient_ref="P-TEST", external_id=session_ref)
vitals = {"spo2": 98, "systolic": 118, "diastolic": 76, "heart_rate": 72, "temperature": 36.6,
          "respiratory_rate": 16, "height_cm": 167, "weight_kg": 62.4}
result = {"tier": "Normal", "tier_rank": 0, "probabilities": {"Normal": 1.0},
          "in_model_range": True, "critical_flags": []}
mid = edge_db.save_assessment(conn, cfg["kiosk_id"], sid, vitals, result, "test-visit")

print(f"queued test visit {mid[:8]} (session {session_ref}) for kiosk {cfg['kiosk_id']}")
print(f"outbox now: {edge_db.queue_depth(conn)}")
print(f"next: python sync_worker.py {CFG.name}")
