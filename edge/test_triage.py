"""Tests for triage_rules, the flag-only flow, the field-test log and the tie-break setting. No broker needed."""
import copy, json, sqlite3, sys, tempfile
from pathlib import Path
HERE = Path(__file__).parent.resolve(); sys.path.insert(0, str(HERE))
import edge_db, field_log, triage_rules
from visit_recorder import VisitRecorder

ok_all = []
def check(name, cond, detail=""):
    ok_all.append(bool(cond)); print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))

T = triage_rules.load_config(HERE / "triage_config.json")
calm = {"spo2": 97, "systolic": 120, "heart_rate": 75, "temperature": 36.8, "respiratory_rate": 16}

# ---- decide (flags only) ----
d = triage_rules.decide([0.02, 0.03, 0.05, 0.90], calm, T)
check("confident result is OK", d["alert_level"] == "OK" and d["tier_rank"] == 3 and not d["check"])
d = triage_rules.decide([0.05, 0.10, 0.40, 0.45], calm, T)
check("45/40 split is flagged LOW_CONFIDENCE and keeps its tier", d["alert_level"] == "LOW_CONFIDENCE" and d["tier_rank"] == 3)
crit = dict(calm, spo2=89)
d = triage_rules.decide([0.95, 0.03, 0.01, 0.01], crit, T)
check("critical vital with a confident Normal -> check flag, tier stays Normal", d["check"] and d["tier_rank"] == 0 and d["alert_level"] == "OK" and d["critical_flags"] == ["spo2<=91"])
d = triage_rules.decide([0.01, 0.02, 0.05, 0.92], crit, T)
check("critical vital never changes a confident High tier", not d["check"] and d["tier_rank"] == 3 and d["critical_flags"])
check("no retake or staff-review setting remains in the config", "max_retries" not in T and "staff_review_priority" not in T)

# ---- critical limits (at a limit counts) ----
L = T["critical_limits"]
fl = lambda **kw: triage_rules.critical_flags(dict(calm, **kw), L)
check("a reading exactly on a low limit is critical", fl(spo2=91) == ["spo2<=91"] and fl(spo2=92) == [])
check("a reading exactly on a high limit is critical", fl(systolic=180) == ["systolic>=180"] and fl(temperature=39.1) == ["temperature>=39.1"] and fl(temperature=39.0) == [])
check("SpO2 has no high limit", fl(spo2=100) == [])
check("systolic 179 is not critical, 180 is (Philippine severe-BP limit)", fl(systolic=179) == [] and fl(systolic=180) == ["systolic>=180"])
check("diastolic 120 is critical, 119 is not", fl(diastolic=120) == ["diastolic>=120"] and fl(diastolic=119) == [])
check("a missing or None diastolic is skipped, not an error", fl(diastolic=None) == [] and triage_rules.critical_flags({k: v for k, v in calm.items() if k != "diastolic"}, L) == [])

# ---- tie-break ----
A = {"id": "A", "tier_rank": 3, "proba": [0, 0, .01, .99], "wait_min": 3}
B = {"id": "B", "tier_rank": 3, "proba": [0, 0, .12, .88], "wait_min": 12}
C = {"id": "C", "tier_rank": 3, "proba": [0, 0, .02, .98], "wait_min": 20}
D = {"id": "D", "tier_rank": 3, "proba": [0, 0, .03, .97], "wait_min": 5}
M = {"id": "M", "tier_rank": 2, "proba": [0, .01, .99, 0], "wait_min": 90}
ids = lambda r, tb=None, cfg=T: [x["id"] for x in triage_rules.queue_order(r, cfg, tb)]
check("probability rule: higher P(High) first", ids([B, A], "probability") == ["A", "B"])
check("wait rule: longest waiting first", ids([A, B], "wait") == ["B", "A"])
check("probability rule is strict: 98% goes before 97% even if 97% waited much longer", ids([dict(D, wait_min=99), C], "probability") == ["C", "D"])
Tb = dict(T, prob_step=0.05)
check("optional band setting: with prob_step 0.05, 98% and 97% count as equal and the longer wait goes first", ids([dict(D, wait_min=99), C], "probability", Tb) == ["D", "C"])
check("equal probabilities fall to waiting time", ids([dict(C, id="E", wait_min=1), C], "probability") == ["C", "E"])
check("tier always comes first under both rules", ids([M, A], "probability") == ["A", "M"] and ids([M, A], "wait") == ["A", "M"])
Tw = dict(T, tie_break="wait")
check("default rule is probability (with max_bypass 2), and switching tie_break in the config switches the active order", T["tie_break"] == "probability" and T["max_bypass"] == 2 and ids([A, B]) == ["A", "B"] and ids([A, B], cfg=Tw) == ["B", "A"])
# bounded waiting: an old, less certain patient can be passed by at most 2 newer patients of the same tier
OLD = {"id": "OLD", "tier_rank": 2, "proba": [0, .1, .6, .3], "wait_min": 40}
N1 = {"id": "N1", "tier_rank": 2, "proba": [0, 0, .95, .05], "wait_min": 10}
N2 = {"id": "N2", "tier_rank": 2, "proba": [0, 0, .90, .10], "wait_min": 8}
N3 = {"id": "N3", "tier_rank": 2, "proba": [0, 0, .85, .15], "wait_min": 5}
N4 = {"id": "N4", "tier_rank": 2, "proba": [0, 0, .80, .20], "wait_min": 2}
check("bounded waiting: after 2 newer patients pass, the old patient goes next", ids([N4, OLD, N2, N3, N1]) == ["N1", "N2", "OLD", "N3", "N4"])
check("bounded waiting: passes that already happened (bypassed=2) send the patient next at once", ids([dict(OLD, bypassed=2), N1, N2]) == ["OLD", "N1", "N2"])
check("bounded waiting: with fewer than 2 newer patients, the probability order is unchanged", ids([OLD, N1]) == ["N1", "OLD"])
check("bounded waiting never crosses tiers: a High patient still goes before an old Medium one", ids([dict(OLD, bypassed=5), A]) == ["A", "OLD"])
check("without max_bypass the probability order is strict", ids([N4, OLD, N2, N3, N1], cfg={k: v for k, v in T.items() if k != "max_bypass"}) == ["N1", "N2", "N3", "N4", "OLD"])
U = {"id": "U", "tier_rank": 3, "proba": [0, 0, .45, .45], "wait_min": 99, "alert_level": "LOW_CONFIDENCE"}
q = triage_rules.queue_both([A, B, U], T)
check("a low-confidence record keeps its tier place and is ranked with the rest", set(q["probability"]) == {"A", "B", "U"} and q["n_low_confidence"] == 1 and q["n_queue"] == 3)
check("low-confidence flag never lifts a lower tier above a higher one", triage_rules.queue_order([M, dict(U, tier_rank=1, proba=[0, .45, .45, .1])], T)[0]["id"] == M["id"] if M["tier_rank"] > 1 else True)
try:
    triage_rules.load_config(HERE / "edge_config.json"); bad = False
except Exception:
    bad = True
check("a config without a valid tie_break is refused", bad)

# ---- low-confidence reading through the recorder ----
cfg = json.load(open(HERE / "edge_config.example.json"))
tmp = tempfile.mkdtemp(); cfg.update(base_dir=str(HERE), db_path=f"{tmp}/t.db")
rec = VisitRecorder(cfg)
def visit(spo2=98, sys_=120, hr=75, temp=36.8, rr=16, **kw):
    r = {"finger": {"rr_bpm": rr, "level": "CONFIRMED", "heart_rate_bpm": hr, "spo2_pct": spo2, "error": None},
         "bp": {"sys": sys_, "dia": 78, "pul": hr, "error": None}, "temp": {"object_c": temp, "ambient_c": 27.0, "error": None},
         "height": {"height_cm": 160.0}, "weight": {"weight_kg": 60.0}}
    r.update(kw); return r
unsure = dict(spo2=93, sys_=109, hr=95, temp=37.8, rr=17)       # a real record the model scores about 49% Low / 51% Medium
o1 = rec.record(visit(**unsure), external_session_id="W1")
db = sqlite3.connect(cfg["db_path"])
check("an low-confidence reading is saved and sent, not retaken", o1["status"] == "saved" and o1["alert_level"] == "LOW_CONFIDENCE", f"conf {o1.get('confidence'):.2f}")
row = db.execute("SELECT priority, payload FROM outbox").fetchone()
check("its priority is the tier rank, with the low-confidence flag in the payload", row[0] in (0, 1, 2, 3) and json.loads(row[1])["alert_level"] == "LOW_CONFIDENCE" and row[0] == json.loads(row[1])["priority"])
cr = rec.record(visit(spo2=89))
check("critical vital is reported, priority unchanged by it", cr["critical_flags"] and db.execute("SELECT priority FROM outbox WHERE message_id=?", (cr["message_id"],)).fetchone()[0] == triage_rules.CLASSES.index(cr["tier"]))
levels = [r[0] for r in db.execute("SELECT alert_level FROM field_log ORDER BY log_id")]
check("every classification is logged", len(levels) == 2 and levels[0] == "LOW_CONFIDENCE", str(levels))

# ---- logging both orders, staff feedback, summary, export ----
conn = edge_db.connect(cfg["db_path"])
sick = visit(spo2=88, sys_=85, hr=134, temp=39.5, rr=28)
a = rec.record(sick); b = rec.record(visit(spo2=89, sys_=88, hr=130, temp=39.2, rr=26)); c_ = rec.record(visit())
snaps = db.execute("SELECT COUNT(*), SUM(differs) FROM queue_snapshots").fetchone()
check("queue snapshots are logged when two or more are queued", snaps[0] >= 1, f"{snaps[0]} snapshots")
sc = db.execute("SELECT order_critical_first, differs_critical FROM queue_snapshots ORDER BY snapshot_id DESC LIMIT 1").fetchone()
check("snapshots also store the critical-first order and whether it differs", sc[0] is not None and sc[1] in (0, 1))
r1 = field_log.record_staff_decision(conn, a["message_id"][:8], "High", seen_at=edge_db.utc_now(), staff_code="AB")
r2 = field_log.record_staff_decision(conn, c_["message_id"][:8], "Low")
check("staff decision is matched by the first characters of the visit id", r1["kiosk_tier"] == a["tier"] and r2["staff_tier"] == "Low")
try:
    field_log.record_staff_decision(conn, "zzzz", "High"); bad = False
except ValueError:
    bad = True
check("an unknown visit id is refused", bad)
s = field_log.summary(conn)
check("summary counts low-confidence flags and agreement", s["results_low_model_confidence"] == 2 and s["staff_decisions_recorded"] == 2 and s["staff_High_total"] == 1, str(s))
out = Path(tmp) / "export"; field_log.export_all(conn, out)
check("export writes the three CSV files and a summary", all((out / f).exists() for f in ("field_log.csv", "staff_feedback.csv", "queue_snapshots.csv", "summary.txt")))
check("the log holds no patient_ref column", "patient_ref" not in (out / "field_log.csv").read_text().splitlines()[0])
Cr = dict(B, id="X", critical=True, wait_min=1)
Nc = dict(A, id="Y", critical=False, wait_min=50)
Tc = dict(T, critical_first=True)
check("critical_first off (default): a critical vital does not change the order", ids([Cr, Nc]) == ["Y", "X"])
check("critical_first on: a patient with a critical vital goes first inside the tier", ids([Nc, Cr], cfg=Tc) == ["X", "Y"])
check("critical_first on never lifts a lower tier above a higher one", ids([dict(M, critical=True), Nc], cfg=Tc) == ["Y", "M"])
check("critical_first works with the wait rule too", ids([Nc, Cr], "wait", Tc) == ["X", "Y"])

qc = triage_rules.queue_both([Nc, Cr], T)
check("queue_both also returns the critical-first order and whether it differs", qc["order_critical_first"] == ["X", "Y"] and qc["differs_critical"] is True)
qn = triage_rules.queue_both([A, B], T)
check("critical-first order is unchanged when nobody has a critical value", qn["differs_critical"] is False)
print(f"\n{sum(ok_all)}/{len(ok_all)} checks passed"); sys.exit(0 if all(ok_all) else 1)
