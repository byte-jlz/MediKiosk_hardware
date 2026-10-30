"""SQLite helpers for the Medi-Kiosk edge node."""
import json, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2


def utc_now(offset_s=0):
    t = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")      # a committed row survives a power cut
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA.read_text())
    # small upgrades for databases created by an older version of this file
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(assessments)")}
    if "quality" not in cols:
        conn.execute("ALTER TABLE assessments ADD COLUMN quality TEXT NOT NULL DEFAULT '{}'")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "external_id" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN external_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_ext ON sessions(external_id, started_at)")
    return conn


class _Tx:
    """with _Tx(conn): ...  -> BEGIN IMMEDIATE / COMMIT, or ROLLBACK on error."""
    def __init__(self, conn): self.conn = conn
    def __enter__(self): self.conn.execute("BEGIN IMMEDIATE"); return self.conn
    def __exit__(self, et, ev, tb):
        self.conn.execute("ROLLBACK" if et else "COMMIT")
        return False


# ---------- sessions and raw readings ----------
def start_session(conn, patient_ref=None, qr_jti=None, external_id=None):
    sid = str(uuid.uuid4())
    kind = "patient" if patient_ref else "guest"          # 'guest' = no patient identity known at the edge
    with _Tx(conn):
        conn.execute("""INSERT INTO sessions(session_id,session_type,patient_ref,qr_jti,external_id,started_at)
                        VALUES(?,?,?,?,?,?)""", (sid, kind, patient_ref, qr_jti, external_id, utc_now()))
    return sid


def find_session(conn, external_id):
    """Newest session with this wizard session id, or None."""
    return conn.execute("SELECT * FROM sessions WHERE external_id=? ORDER BY started_at DESC LIMIT 1",
                        (external_id,)).fetchone()


def record_reading(conn, session_id, vital, value, quality="ok"):
    with _Tx(conn):
        conn.execute("INSERT INTO readings(session_id,vital,value,quality,captured_at) VALUES(?,?,?,?,?)",
                     (session_id, vital, float(value), quality, utc_now()))


def latest_vitals(conn, session_id):
    """Most recent accepted reading per vital for this session."""
    rows = conn.execute("""SELECT vital, value FROM readings
                           WHERE session_id=? AND quality='ok' AND reading_id IN
                             (SELECT MAX(reading_id) FROM readings
                              WHERE session_id=? AND quality='ok' GROUP BY vital)""",
                        (session_id, session_id)).fetchall()
    return {r["vital"]: r["value"] for r in rows}


# ---------- scan results published by the kiosk agent ----------
def save_scan(conn, scan_id, external_session_id, vital, ok, result_json):
    """Store one agent result. Returns False if this scan_id was already stored (duplicate delivery)."""
    with _Tx(conn):
        cur = conn.execute("INSERT OR IGNORE INTO scans(scan_id,external_session_id,vital,ok,result,received_at) VALUES(?,?,?,?,?,?)",
                           (scan_id, external_session_id, vital, int(bool(ok)), result_json, utc_now()))
    return cur.rowcount == 1


def latest_ok_scans(conn, external_session_id):
    """{vital: the agent's 'values' dict} using the newest successful scan of each vital."""
    out = {}
    for r in conn.execute("""SELECT vital, result FROM scans WHERE external_session_id=? AND ok=1
                             ORDER BY received_at ASC, rowid ASC""", (external_session_id,)):
        values = json.loads(r["result"]).get("values")
        if values:
            out[r["vital"]] = values
    return out


def session_completed(conn, external_session_id):
    s = find_session(conn, external_session_id)
    return bool(s and s["status"] == "complete")


# ---------- classified result + outbox, in ONE transaction ----------
def save_assessment(conn, kiosk_id, session_id, vitals, result, model_version, quality=None):
    message_id = str(uuid.uuid4())
    now = utc_now()
    quality = quality or {}
    h, w = vitals.get("height_cm"), vitals.get("weight_kg")
    bmi = round(w / (h / 100) ** 2, 1) if h and w else None
    with _Tx(conn):
        s = conn.execute("SELECT session_type, patient_ref, external_id FROM sessions WHERE session_id=?",
                         (session_id,)).fetchone()
        conn.execute("""INSERT INTO assessments(assessment_id,session_id,spo2,systolic,diastolic,heart_rate,
                        temperature,respiratory_rate,height_cm,weight_kg,bmi,tier,tier_rank,probabilities,
                        in_model_range,critical_flags,model_version,captured_at,quality)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (message_id, session_id, vitals["spo2"], vitals["systolic"], vitals.get("diastolic"),
                      vitals["heart_rate"], vitals["temperature"], vitals["respiratory_rate"], h, w, bmi,
                      result["tier"], result["tier_rank"], json.dumps(result["probabilities"]),
                      int(result["in_model_range"]), json.dumps(result["critical_flags"]), model_version, now,
                      json.dumps(quality)))
        payload = {
            "schema_version": SCHEMA_VERSION, "message_id": message_id, "kiosk_id": kiosk_id,
            "session_type": s["session_type"], "patient_ref": s["patient_ref"],
            "external_session_id": s["external_id"], "captured_at": now,
            "vitals": {k: vitals.get(k) for k in ("spo2", "systolic", "diastolic", "heart_rate",
                                                  "temperature", "respiratory_rate")},
            "body": {"height_cm": h, "weight_kg": w, "bmi": bmi},
            "urgency": result["tier"], "priority": result["tier_rank"],
            "probabilities": result["probabilities"], "in_model_range": result["in_model_range"],
            "critical_flags": result["critical_flags"], "model_version": model_version, "quality": quality,
        }
        conn.execute("""INSERT INTO outbox(message_id,topic,payload,priority,created_at)
                        VALUES(?,?,?,?,?)""",
                     (message_id, f"medikiosk/{kiosk_id}/vitals/{result['tier'].lower()}",
                      json.dumps(payload), result["tier_rank"], now))
        conn.execute("UPDATE sessions SET status='complete', ended_at=? WHERE session_id=?", (now, session_id))
    return message_id


# ---------- outbox operations used by the sync worker ----------
def recover(conn):
    """After a crash or power cut: anything left 'sending' goes back to 'pending'."""
    with _Tx(conn):
        conn.execute("UPDATE outbox SET status='pending' WHERE status='sending'")


def claim_next(conn):
    """Highest tier first, then oldest first. One message in flight at a time keeps the order exact."""
    with _Tx(conn):
        row = conn.execute("""SELECT message_id, topic, payload, attempts FROM outbox
                              WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                              ORDER BY priority DESC, created_at ASC LIMIT 1""", (utc_now(),)).fetchone()
        if row:
            conn.execute("UPDATE outbox SET status='sending', attempts=attempts+1 WHERE message_id=?",
                         (row["message_id"],))
    return row


def mark_synced(conn, message_id):
    with _Tx(conn):
        conn.execute("UPDATE outbox SET status='synced', synced_at=? WHERE message_id=? AND status!='synced'",
                     (utc_now(), message_id))


def mark_retry(conn, message_id, delay_s):
    with _Tx(conn):
        conn.execute("UPDATE outbox SET status='pending', next_retry_at=? WHERE message_id=? AND status='sending'",
                     (utc_now(delay_s), message_id))


def queue_depth(conn):
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}


def purge(conn, retention_days=7):
    """Delete data older than the retention window, but only once it has been synced."""
    cutoff = utc_now(-retention_days * 86400)
    with _Tx(conn):
        conn.execute("DELETE FROM outbox WHERE status='synced' AND synced_at < ?", (cutoff,))
        conn.execute("""DELETE FROM assessments WHERE captured_at < ?
                        AND assessment_id NOT IN (SELECT message_id FROM outbox)""", (cutoff,))
        conn.execute("""DELETE FROM readings WHERE session_id IN
                        (SELECT session_id FROM sessions WHERE started_at < ?
                         AND session_id NOT IN (SELECT session_id FROM assessments))""", (cutoff,))
        conn.execute("""DELETE FROM sessions WHERE started_at < ?
                        AND session_id NOT IN (SELECT session_id FROM assessments)
                        AND session_id NOT IN (SELECT session_id FROM readings)""", (cutoff,))
        conn.execute("DELETE FROM scans WHERE received_at < ?", (cutoff,))
        conn.execute("DELETE FROM qr_seen WHERE seen_at < ?", (cutoff,))
