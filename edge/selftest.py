"""End-to-end self-test of the edge node against a real local Mosquitto broker.
Run:  python3 selftest.py     (needs: mosquitto, paho-mqtt, PyJWT, cryptography, scikit-learn)"""
import json, os, shutil, signal, sqlite3, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as ser

HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))
import edge_db
from classifier import Classifier, ReadingRejected
from qr_login import verify_qr, QRRejected

TMP = Path(tempfile.mkdtemp(prefix="medikiosk_test_"))
cfg = json.load(open(HERE / "edge_config.example.json"))
cfg.update(base_dir=str(HERE), db_path=str(TMP / "edge.db"), ack_timeout_s=2, kiosk_id="kiosk01")
cfg["broker"]["port"] = 18830
CFG_PATH = TMP / "edge_config.json"; json.dump(cfg, open(CFG_PATH, "w"))
RESULTS = []
procs = {}


def check(name, ok, detail=""):
    RESULTS.append(ok); print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def start(name, *cmd):
    procs[name] = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(name, sig=signal.SIGTERM):
    p = procs.pop(name, None)
    if p: p.send_signal(sig); p.wait(timeout=10)


def q(db, sql, args=()):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    try: return c.execute(sql, args).fetchall()
    finally: c.close()


def wait_for(fn, timeout=40):
    t = time.time()
    while time.time() - t < timeout:
        if fn(): return True
        time.sleep(0.3)
    return False


vit = {  # (spo2, systolic, diastolic, hr, temp, rr)
    "Normal": dict(spo2=98, systolic=120, diastolic=78, heart_rate=75, temperature=36.8, respiratory_rate=16),
    "Low": dict(spo2=95, systolic=112, diastolic=74, heart_rate=92, temperature=37.6, respiratory_rate=19),
    "Medium": dict(spo2=94, systolic=105, diastolic=70, heart_rate=95, temperature=38.2, respiratory_rate=22),
    "High": dict(spo2=88, systolic=85, diastolic=55, heart_rate=135, temperature=39.5, respiratory_rate=28)}


def make_patient(conn, clf, tier, ref=None):
    sid = edge_db.start_session(conn, patient_ref=ref)
    v = dict(vit[tier], height_cm=160, weight_kg=60)
    for k, val in v.items(): edge_db.record_reading(conn, sid, k, val)
    got = edge_db.latest_vitals(conn, sid)
    return edge_db.save_assessment(conn, "kiosk01", sid, got, clf.classify(got), cfg["model_version"]), clf.classify(got)["tier"]


try:
    # ---- broker + stand-in backend ----
    (TMP / "mosq.conf").write_text(f"listener 18830 127.0.0.1\nallow_anonymous true\npersistence true\npersistence_location {TMP}/\n")
    start("broker", "mosquitto", "-c", str(TMP / "mosq.conf")); time.sleep(1.5)
    start("backend", sys.executable, "fake_backend.py", "127.0.0.1", str(TMP / "received.db"), "18830"); time.sleep(1.5)
    RDB = str(TMP / "received.db")

    # ---- 1. classifier ----
    clf = Classifier(cfg)
    got = {t: clf.classify(v)["tier"] for t, v in vit.items()}
    check("classifier gives expected tiers", got == {t: t for t in vit}, str(got))
    try: clf.classify(dict(vit["Normal"], spo2=0)); check("impossible SpO2=0 is rejected", False)
    except ReadingRejected: check("impossible SpO2=0 is rejected", True)
    r = clf.classify(dict(vit["Normal"], systolic=200))
    check("SBP 200 is marked outside the model's training range", (not r["in_model_range"]) and r["outside_range"] == ["systolic"])
    check("critical-value flag fires for SpO2 88", "spo2<90" in clf.classify(vit["High"])["critical_flags"])

    # ---- 2. QR login ----
    priv = Ed25519PrivateKey.generate(); other = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo)
    def token(key, exp_s=120, jti="t1"):
        now = datetime.now(timezone.utc)
        return jwt.encode({"sub": "PT-0042", "jti": jti, "aud": "medikiosk", "iat": now, "exp": now + timedelta(seconds=exp_s)}, key, algorithm="EdDSA")
    conn = edge_db.connect(cfg["db_path"])
    ref, jti = verify_qr(conn, token(priv, jti="a1"), pub)
    check("valid QR accepted", ref == "PT-0042")
    for name, tk in [("replayed QR rejected", token(priv, jti="a1")), ("expired QR rejected", token(priv, exp_s=-300, jti="a2")),
                     ("QR signed with wrong key rejected", token(other, jti="a3"))]:
        try: verify_qr(conn, tk, pub); check(name, False)
        except QRRejected: check(name, True)

    # ---- 3. offline buffering + priority order ----
    order = ["Low"] * 6 + ["Normal"] * 3 + ["Medium"] + ["High"] * 2     # High created LAST on purpose
    ids = [make_patient(conn, clf, t, ref="PT-0042" if i == 0 else None)[0] for i, t in enumerate(order)]
    check("12 readings saved in outbox before any network", edge_db.queue_depth(conn).get("pending") == len(order))
    start("worker", sys.executable, "sync_worker.py", str(CFG_PATH))
    ok = wait_for(lambda: len(q(RDB, "SELECT 1 FROM got")) == len(order))
    check("all buffered readings reached the backend", ok, f"{len(q(RDB, 'SELECT 1 FROM got'))}/{len(order)}")
    arr = [r["urgency"] for r in q(RDB, "SELECT urgency FROM arrivals ORDER BY seq")]
    rank = {"Normal": 0, "Low": 1, "Medium": 2, "High": 3}
    check("sent in priority order (High first)", [rank[a] for a in arr] == sorted([rank[a] for a in arr], reverse=True), " > ".join(arr[:4]) + " ...")
    check("no duplicates stored", len(set(r["message_id"] for r in q(RDB, "SELECT message_id FROM got"))) == len(order))
    ok = wait_for(lambda: edge_db.queue_depth(edge_db.connect(cfg["db_path"])).get("synced") == len(order))
    check("outbox rows marked synced after ack", ok)

    # ---- 4. backend down, then back ----
    stop("backend"); time.sleep(1)
    extra = [make_patient(conn, clf, t)[0] for t in ["Low", "High", "Normal"]]
    time.sleep(7)
    st = edge_db.queue_depth(edge_db.connect(cfg["db_path"]))
    check("while backend is down, readings stay safely in the buffer", st.get("synced") == len(order) and (st.get("pending", 0) + st.get("sending", 0)) == 3, str(st))
    start("backend", sys.executable, "fake_backend.py", "127.0.0.1", RDB, "18830")
    ok = wait_for(lambda: len(q(RDB, "SELECT 1 FROM got")) == len(order) + 3, 60)
    check("after backend returns, nothing was lost", ok, f"{len(q(RDB, 'SELECT 1 FROM got'))}/{len(order)+3}")
    check("still no duplicate rows stored", len(q(RDB, "SELECT message_id FROM got GROUP BY message_id HAVING COUNT(*)>1")) == 0,
          f"duplicate deliveries seen and ignored: {q(RDB, 'SELECT COUNT(*) n FROM arrivals WHERE duplicate=1')[0]['n']}")
    ok = wait_for(lambda: edge_db.queue_depth(edge_db.connect(cfg["db_path"])).get("synced") == len(order) + 3, 40)
    check("all rows end up synced", ok)

    # ---- 5. crash recovery: worker killed hard while a row is 'sending' ----
    stop("worker", signal.SIGKILL)
    c2 = edge_db.connect(cfg["db_path"]); mid, _ = make_patient(c2, clf, "High")
    c2.execute("UPDATE outbox SET status='sending' WHERE message_id=?", (mid,))
    start("worker", sys.executable, "sync_worker.py", str(CFG_PATH))
    check("row stuck in 'sending' after a crash is recovered and delivered",
          wait_for(lambda: q(RDB, "SELECT 1 FROM got WHERE message_id=?", (mid,)) != []))

    # ---- 6. duplicate delivery is harmless ----
    before = len(q(RDB, "SELECT 1 FROM got"))
    c2.execute("UPDATE outbox SET status='pending', synced_at=NULL WHERE message_id=?", (mid,))
    ok = wait_for(lambda: edge_db.queue_depth(edge_db.connect(cfg["db_path"])).get("pending", 0) == 0 and
                  q(RDB, "SELECT COUNT(*) n FROM arrivals WHERE message_id=?", (mid,))[0]["n"] >= 2)
    check("resending an already-stored message is acked and not stored twice", ok and len(q(RDB, "SELECT 1 FROM got")) == before)
    stop("worker")

    # ---- 7. process killed mid-write: no half-saved records ----
    writer = TMP / "writer.py"
    writer.write_text(f"import sys; sys.path.insert(0, r'{HERE}')\nimport json, edge_db\nfrom classifier import Classifier\n"
                      f"cfg=json.load(open(r'{CFG_PATH}')); clf=Classifier(cfg); conn=edge_db.connect(cfg['db_path'])\n"
                      "v={'spo2':98,'systolic':120,'diastolic':78,'heart_rate':75,'temperature':36.8,'respiratory_rate':16}\n"
                      "while True:\n    s=edge_db.start_session(conn)\n    edge_db.save_assessment(conn,'kiosk01',s,v,clf.classify(v),'t')\n")
    w = subprocess.Popen([sys.executable, str(writer)]); time.sleep(3); w.send_signal(signal.SIGKILL); w.wait()
    c3 = edge_db.connect(cfg["db_path"])
    integ = c3.execute("PRAGMA integrity_check").fetchone()[0]
    orphans = c3.execute("SELECT COUNT(*) FROM assessments a LEFT JOIN outbox o ON o.message_id=a.assessment_id WHERE o.message_id IS NULL").fetchone()[0]
    check("database intact after a hard kill during writes", integ == "ok" and orphans == 0,
          f"integrity={integ}, assessments without an outbox row={orphans}, rows written={c3.execute('SELECT COUNT(*) FROM assessments').fetchone()[0]}")
finally:
    for n in list(procs): 
        try: stop(n, signal.SIGKILL)
        except Exception: pass
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)
