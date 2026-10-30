"""Tests visit_recorder with result dicts shaped exactly like measurements.py returns. No broker needed."""
import json, sqlite3, sys, tempfile, copy
from pathlib import Path
HERE = Path(__file__).parent.resolve(); sys.path.insert(0, str(HERE))
from visit_recorder import VisitRecorder

cfg = json.load(open(HERE / "edge_config.example.json"))
tmp = tempfile.mkdtemp(); cfg.update(base_dir=str(HERE), db_path=f"{tmp}/t.db")
rec = VisitRecorder(cfg)
ok_all = []
def check(name, cond, detail=""):
    ok_all.append(cond); print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))

def finger(hr=75, spo2=98, rr=16, level="CONFIRMED"):
    return {"rr_bpm": rr, "level": level, "low": None, "high": None, "reasons": [], "heart_rate_bpm": hr, "hr_samples": 40,
            "spo2_pct": spo2, "spo2_samples": 40, "error": None if level != "NONE" else "no reliable respiratory rate"}
def visit(**kw):
    r = {"finger": finger(), "bp": {"sys": 120, "dia": 78, "pul": 80, "duration_s": 45.8, "error_code": None, "error": None},
         "temp": {"object_c": 36.8, "ambient_c": 27.0, "error": None},
         "height": {"distance_mm": 400, "samples": 40, "height_cm": 160.0, "error": None},
         "weight": {"weight_kg": 60.0, "steadiness_kg": 0.1, "readings": 12, "error": None}}
    r.update(kw); return r

o = rec.record(visit())
check("normal visit is saved as Normal", o["status"] == "saved" and o["tier"] == "Normal", str(o.get("tier")))
o = rec.record(visit(finger=finger(hr=135, spo2=88, rr=28), bp={"sys": 85, "dia": 55, "pul": 130, "error": None},
                     temp={"object_c": 39.5, "ambient_c": 27.0, "error": None}))
check("sick visit is saved as High with critical flags", o["status"] == "saved" and o["tier"] == "High" and len(o["critical_flags"]) >= 3, f"{o['tier']} {o['critical_flags']}")
o = rec.record(visit(finger=finger(rr=17, level="ESTIMATE")))
check("RR ESTIMATE is accepted and flagged", o["status"] == "saved" and o["quality"]["rr_level"] == "ESTIMATE")
o = rec.record(visit(finger=finger(rr=None, level="NONE")))
check("RR NONE -> incomplete, redo the finger step", o["status"] == "incomplete" and o["missing"] == ["respiratory_rate"] and o["redo"] == ["finger"], str(o["redo"]))
sid = o["session_id"]
o2 = rec.record(visit(finger=finger(rr=16)), session_id=sid)
check("after re-measuring, the SAME session completes", o2["status"] == "saved" and o2["session_id"] == sid)
o = rec.record(visit(bp={"sys": None, "dia": None, "pul": None, "error": "ERROR Er E - check the cuff"}))
check("BP error -> incomplete, redo bp", o["status"] == "incomplete" and o["redo"] == ["bp"], str(o["missing"]))
o = rec.record(visit(temp={"object_c": 52.0, "ambient_c": 27.0, "error": None}))
check("impossible temperature (52 C) -> rejected, redo temp", o["status"] == "rejected" and o["redo"] == ["temp"])
o = rec.record(visit(finger=finger(hr=None)))
check("finger HR missing -> falls back to BP pulse", o["status"] == "saved" and o["quality"]["hr_source_used"] == "bp_pulse")
cfg2 = dict(cfg, hr_source="bp_pulse"); r2 = VisitRecorder(cfg2)
o = r2.record(visit(finger=finger(hr=70), bp={"sys": 120, "dia": 78, "pul": 88, "error": None}))
check("hr_source=bp_pulse uses the BP monitor's pulse", o["quality"]["hr_source_used"] == "bp_pulse")
o = rec.record(visit(height={"height_cm": None, "error": "nothing under the sensor"}, weight={"weight_kg": None, "error": "x"}))
check("missing height/weight does not block classification", o["status"] == "saved")
o = rec.record(visit(bp={"sys": 200, "dia": 100, "pul": 80, "error": None}))
check("BP above the training range is classified but marked outside the model's range", o["status"] == "saved" and not o["in_model_range"])

db = sqlite3.connect(cfg["db_path"])
n_ass = db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]; n_out = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
check("every saved visit has exactly one outbox message", n_ass == n_out and n_ass > 0, f"{n_ass} assessments, {n_out} outbox")
check("raw readings are stored (including rejected ones)", db.execute("SELECT COUNT(*) FROM readings WHERE quality='rejected'").fetchone()[0] >= 1)
p = json.loads(db.execute("SELECT payload FROM outbox LIMIT 1").fetchone()[0])
check("payload carries the quality notes", "quality" in p and p["schema_version"] == 2)
print(f"\n{sum(ok_all)}/{len(ok_all)} checks passed"); sys.exit(0 if all(ok_all) else 1)
