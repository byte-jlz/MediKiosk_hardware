"""Turns the results of measurements.py into a saved, classified, queued visit.

    rec = VisitRecorder("edge_config.json")        # once, when the agent starts (loads the model)
    out = rec.record({"finger": measure_finger(...), "bp": measure_bp(...), "temp": measure_temp(...),
                      "height": measure_height(...), "weight": measure_weight(...)})

out["status"] is one of:
    "saved"       classified and queued for sending (out has tier, message_id, critical_flags, ...)
    "incomplete"  a needed vital is missing  -> out["redo"] lists the steps to repeat
    "rejected"    a value is physically impossible -> out["redo"] says which step
After "incomplete"/"rejected", re-measure the steps in out["redo"], put the new results into the same
dict and call rec.record(results, session_id=out["session_id"]) again.
The tier is returned to the caller only: decide yourself what the patient screen may show.
"""
import json
import edge_db
from classifier import Classifier, ReadingRejected

REQUIRED = ["spo2", "systolic", "heart_rate", "temperature", "respiratory_rate"]
STEP_FOR = {"spo2": "finger", "heart_rate": "finger", "respiratory_rate": "finger",
            "systolic": "bp", "diastolic": "bp", "temperature": "temp"}
AGENT_VITAL_FOR = {"finger": "respirationSpo2Hr", "bp": "bloodPressure", "temp": "temperature"}   # names the kiosk agent uses


def build_vitals(results, hr_source="bp_pulse", rr_levels=("CONFIRMED", "ESTIMATE")):
    """measurements.py result dicts -> (vitals for the model, quality notes, missing vital names)."""
    finger, bp, temp = (results.get(k) or {} for k in ("finger", "bp", "temp"))
    hr_finger, hr_bp = finger.get("heart_rate_bpm"), bp.get("pul")
    order = [("finger", hr_finger), ("bp_pulse", hr_bp)]
    if hr_source == "bp_pulse":
        order.reverse()
    hr, hr_used = next(((v, n) for n, v in order if v is not None), (None, None))
    rr_ok = finger.get("level") in rr_levels
    vitals = {"spo2": finger.get("spo2_pct"), "systolic": bp.get("sys"), "diastolic": bp.get("dia"),
              "heart_rate": hr, "temperature": temp.get("object_c"),
              "respiratory_rate": finger.get("rr_bpm") if rr_ok else None,
              "height_cm": (results.get("height") or {}).get("height_cm"),
              "weight_kg": (results.get("weight") or {}).get("weight_kg")}
    quality = {"rr_level": finger.get("level"), "hr_source_used": hr_used,
               "hr_finger": hr_finger, "hr_bp_pulse": hr_bp, "temp_ambient_c": temp.get("ambient_c")}
    return vitals, quality, [v for v in REQUIRED if vitals[v] is None]


class VisitRecorder:
    def __init__(self, cfg_path="edge_config.json"):
        self.cfg = json.load(open(cfg_path)) if isinstance(cfg_path, str) else cfg_path
        self.clf = Classifier(self.cfg)                      # model is loaded once, here

    def record(self, results, patient_ref=None, qr_jti=None, session_id=None, external_session_id=None):
        cfg = self.cfg
        conn = edge_db.connect(cfg["db_path"])               # own connection per call: safe across threads
        try:
            if session_id is None and external_session_id:   # same wizard session -> same edge session
                found = edge_db.find_session(conn, external_session_id)
                session_id = found["session_id"] if found and found["status"] == "active" else None
            sid = session_id or edge_db.start_session(conn, patient_ref, qr_jti, external_session_id)
            vitals, quality, missing = build_vitals(
                results, cfg.get("hr_source", "bp_pulse"), tuple(cfg.get("rr_levels_allowed", ("CONFIRMED", "ESTIMATE"))))
            for vital, val in vitals.items():                # raw values are stored BEFORE classification
                if val is not None:
                    lo, hi = cfg["hard_limits"][vital]
                    edge_db.record_reading(conn, sid, vital, val, "ok" if lo <= val <= hi else "rejected")
            if missing:
                return {"status": "incomplete", "session_id": sid, "missing": missing,
                        "redo": sorted({STEP_FOR[v] for v in missing}),
                        "redo_vitals": sorted({AGENT_VITAL_FOR[STEP_FOR[v]] for v in missing}), "quality": quality}
            try:
                result = self.clf.classify(vitals)
            except ReadingRejected as e:
                return {"status": "rejected", "session_id": sid, "vital": e.vital, "value": e.value,
                        "redo": [STEP_FOR[e.vital]], "redo_vitals": [AGENT_VITAL_FOR[STEP_FOR[e.vital]]],
                        "quality": quality}
            mid = edge_db.save_assessment(conn, cfg["kiosk_id"], sid, vitals, result, cfg["model_version"], quality)
            return {"status": "saved", "session_id": sid, "message_id": mid, "tier": result["tier"],
                    "probabilities": result["probabilities"], "in_model_range": result["in_model_range"],
                    "critical_flags": result["critical_flags"], "quality": quality}
        finally:
            conn.close()

    def abort(self, session_id):
        conn = edge_db.connect(self.cfg["db_path"])
        try:
            conn.execute("UPDATE sessions SET status='aborted', ended_at=? WHERE session_id=? AND status='active'",
                         (edge_db.utc_now(), session_id))
        finally:
            conn.close()
