"""Field-test log: one row per classification attempt, the staff's own decision for the same visit, and the queue
as it stood (both tie-break orders). Nothing here changes a tier, the queue or what is sent.

Staff decisions (the field-test ground truth) can be added from a terminal:
    python field_log.py feedback <db> <visit id (first 8 characters are enough)> <High|Medium|Low|Normal> [--seen] [--note "..."] [--staff AB]
Results after the test:
    python field_log.py summary <db>
    python field_log.py export  <db> <folder>        # field_log.csv, staff_feedback.csv, queue_snapshots.csv, summary.txt
No names or contact details are stored here; patient_ref stays in the sessions table only.
"""
import csv, json, sys
from datetime import datetime, timezone
from pathlib import Path

import edge_db
import triage_rules

TIERS = triage_rules.CLASSES


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def log_attempt(conn, kiosk_id, session_id, vitals, quality, result, decision, tcfg, model_version, message_id=None):
    n = conn.execute("SELECT COUNT(*) FROM field_log WHERE session_id=?", (session_id,)).fetchone()[0] + 1
    h, w = vitals.get("height_cm"), vitals.get("weight_kg")
    bmi = round(w / (h / 100) ** 2, 1) if h and w else None
    p = [result["probabilities"][c] for c in TIERS]
    with edge_db._Tx(conn):
        conn.execute("""INSERT INTO field_log(session_id,message_id,attempt_no,captured_at,kiosk_id,model_version,triage_status,
            spo2,systolic,diastolic,heart_rate,temperature,respiratory_rate,height_cm,weight_kg,bmi,
            p_normal,p_low,p_medium,p_high,tier,tier_rank,confidence,alert_level,reason,check_flag,critical_flags,
            in_model_range,tie_break_rule,rr_level,hr_source)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, message_id, n, edge_db.utc_now(), kiosk_id, model_version, tcfg["status"][:40],
             vitals["spo2"], vitals["systolic"], vitals.get("diastolic"), vitals["heart_rate"], vitals["temperature"],
             vitals["respiratory_rate"], h, w, bmi, *p, result["tier"], result["tier_rank"], decision["confidence"],
             decision["alert_level"], decision["reason"], int(decision["check"]), json.dumps(decision["critical_flags"]),
             int(result["in_model_range"]), tcfg["tie_break"], quality.get("rr_level"), quality.get("hr_source_used")))


def pending_records(conn, window_h=12):
    """Visits that are waiting: classified, not yet marked as seen by staff, from the last window_h hours."""
    now = datetime.now(timezone.utc)
    rows = conn.execute("""SELECT message_id, captured_at, tier_rank, p_normal, p_low, p_medium, p_high, alert_level, critical_flags FROM field_log
        WHERE message_id IS NOT NULL AND alert_level IN ('OK','LOW_CONFIDENCE')
        AND message_id NOT IN (SELECT message_id FROM staff_feedback WHERE seen_at IS NOT NULL)""").fetchall()
    out = []
    for r in rows:
        wait = (now - _parse(r["captured_at"])).total_seconds() / 60
        if wait <= window_h * 60:
            out.append({"id": r["message_id"], "tier_rank": r["tier_rank"], "alert_level": r["alert_level"], "wait_min": wait, "critical": r["critical_flags"] not in (None, "", "[]"),
                        "proba": [r["p_normal"], r["p_low"], r["p_medium"], r["p_high"]]})
    return out


def log_queue_snapshot(conn, tcfg):
    """Record both tie-break orders for the current queue. Skipped when fewer than two patients are queued (nothing to order)."""
    q = triage_rules.queue_both(pending_records(conn), tcfg)
    if q["n_queue"] < 2:
        return None
    with edge_db._Tx(conn):
        conn.execute("""INSERT INTO queue_snapshots(taken_at,active_rule,n_queue,n_low_confidence,order_probability,order_wait,differs,order_critical_first,differs_critical)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (edge_db.utc_now(), q["active"], q["n_queue"], q["n_low_confidence"],
                                                   json.dumps(q["probability"]), json.dumps(q["wait"]), int(q["differs"]),
                                                   json.dumps(q["order_critical_first"]), int(q["differs_critical"])))
    return q


def record_staff_decision(conn, message_id, staff_tier, seen_at=None, note=None, staff_code=None):
    """message_id may be the first characters of the visit id. Raises if it is missing or ambiguous."""
    if staff_tier not in TIERS:
        raise ValueError(f"staff_tier must be one of {TIERS}")
    rows = conn.execute("SELECT assessment_id, tier FROM assessments WHERE assessment_id LIKE ?", (message_id + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError(f"visit id '{message_id}' matches {len(rows)} visits")
    mid, kiosk_tier = rows[0]["assessment_id"], rows[0]["tier"]
    with edge_db._Tx(conn):
        conn.execute("""INSERT INTO staff_feedback(message_id,recorded_at,staff_tier,seen_at,override,note,staff_code)
                        VALUES(?,?,?,?,?,?,?)""", (mid, edge_db.utc_now(), staff_tier, seen_at, int(staff_tier != kiosk_tier), note, staff_code))
    return {"message_id": mid, "kiosk_tier": kiosk_tier, "staff_tier": staff_tier, "override": staff_tier != kiosk_tier}


def summary(conn):
    """Headline numbers for the field-test report. Under-triage = the kiosk tier is lower than the staff tier."""
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]
    visits = one("SELECT COUNT(DISTINCT session_id) FROM field_log")
    final = one("SELECT COUNT(*) FROM field_log WHERE message_id IS NOT NULL")
    s = {"visits": visits, "visits_with_a_result": final,
         "results_low_model_confidence": one("SELECT COUNT(*) FROM field_log WHERE alert_level='LOW_CONFIDENCE'"),
         "results_check_flag (critical vital, model says Normal/Low)": one("SELECT COUNT(*) FROM field_log WHERE check_flag=1"),
         "results_with_critical_vital": one("SELECT COUNT(*) FROM field_log WHERE message_id IS NOT NULL AND critical_flags != '[]'"),
         "results_outside_training_range": one("SELECT COUNT(*) FROM field_log WHERE message_id IS NOT NULL AND in_model_range=0")}
    fb = conn.execute("""SELECT f.tier AS kiosk, f.tier_rank AS kr, f.alert_level AS al, s.staff_tier AS staff FROM staff_feedback s
        JOIN field_log f ON f.message_id=s.message_id""").fetchall()
    rank = {t: i for i, t in enumerate(TIERS)}
    n = len(fb)
    s["staff_decisions_recorded"] = n
    if n:
        s["kiosk_matches_staff"] = sum(r["kiosk"] == r["staff"] for r in fb)
        s["kiosk_lower_than_staff (under-triage)"] = sum(r["kr"] < rank[r["staff"]] for r in fb)
        s["kiosk_higher_than_staff (over-triage)"] = sum(r["kr"] > rank[r["staff"]] for r in fb)
        s["staff_High_not_High_on_kiosk"] = sum(r["staff"] == "High" and r["kiosk"] != "High" for r in fb)
        s["staff_High_total"] = sum(r["staff"] == "High" for r in fb)
        for lab, al in (("unflagged", "OK"), ("low_model_confidence", "LOW_CONFIDENCE")):       # evidence for the 80% cut-off
            sub = [r for r in fb if r["al"] == al]
            s[f"kiosk_matches_staff_among_{lab}"] = f"{sum(r['kiosk'] == r['staff'] for r in sub)}/{len(sub)}"
    snaps = one("SELECT COUNT(*) FROM queue_snapshots")
    s["queue_snapshots"] = snaps
    s["snapshots_where_the_two_tie_break_orders_differ"] = one("SELECT COUNT(*) FROM queue_snapshots WHERE differs=1")
    s["snapshots_where_critical_first_would_change_the_order"] = one("SELECT COUNT(*) FROM queue_snapshots WHERE differs_critical=1")
    return s


def export_all(conn, folder):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    for name, q in (("field_log", "SELECT * FROM field_log ORDER BY log_id"), ("staff_feedback", "SELECT * FROM staff_feedback ORDER BY feedback_id"),
                    ("queue_snapshots", "SELECT * FROM queue_snapshots ORDER BY snapshot_id")):
        cur = conn.execute(q); cols = [d[0] for d in cur.description]
        with open(folder / f"{name}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(cols); w.writerows([tuple(r) for r in cur])
    s = summary(conn)
    (folder / "summary.txt").write_text("\n".join(f"{k}: {v}" for k, v in s.items()) + "\n")
    return s


def _main(argv):
    if len(argv) >= 3 and argv[0] == "summary":
        for k, v in summary(edge_db.connect(argv[1])).items(): print(f"{k}: {v}")
    elif len(argv) >= 3 and argv[0] == "export":
        export_all(edge_db.connect(argv[1]), argv[2]); print(f"written to {argv[2]}")
    elif len(argv) >= 4 and argv[0] == "feedback":
        opts = argv[4:]
        seen = edge_db.utc_now() if "--seen" in opts else None
        note = opts[opts.index("--note") + 1] if "--note" in opts else None
        staff = opts[opts.index("--staff") + 1] if "--staff" in opts else None
        print(record_staff_decision(edge_db.connect(argv[1]), argv[2], argv[3], seen, note, staff))
    else:
        print(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
