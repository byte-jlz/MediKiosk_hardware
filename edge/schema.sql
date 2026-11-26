-- Medi-Kiosk edge database (SQLite on the Raspberry Pi)
-- All timestamps are UTC ISO-8601 text, e.g. 2026-10-30T08:15:02.123Z

CREATE TABLE IF NOT EXISTS kiosk_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per kiosk visit. patient_ref is the opaque ID from the QR token (NULL for guests).
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    session_type TEXT NOT NULL CHECK (session_type IN ('patient','guest')),
    patient_ref  TEXT,
    qr_jti       TEXT,
    external_id  TEXT,                      -- the kiosk wizard's session id (the backend links the patient with it)
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','complete','aborted')),
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);

-- Every result message the kiosk agent publishes (one row per scan). Keeping them here means a restart
-- in the middle of a visit loses nothing, and a duplicate delivery is ignored.
CREATE TABLE IF NOT EXISTS scans (
    scan_id             TEXT PRIMARY KEY,       -- the agent's job id
    external_session_id TEXT NOT NULL,
    vital               TEXT NOT NULL,          -- bmi / respirationSpo2Hr / bloodPressure / temperature
    ok                  INTEGER NOT NULL,
    result              TEXT NOT NULL,          -- JSON: the agent's result message
    received_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_session ON scans(external_session_id, vital, received_at);

-- QR tokens already used (stops a screenshot of a QR code from being replayed).
CREATE TABLE IF NOT EXISTS qr_seen (
    jti     TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);

-- Every raw measurement, saved the moment a sensor produces it (before any classification).
CREATE TABLE IF NOT EXISTS readings (
    reading_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    vital       TEXT NOT NULL CHECK (vital IN
                  ('spo2','systolic','diastolic','heart_rate','temperature',
                   'respiratory_rate','height_cm','weight_kg')),
    value       REAL NOT NULL,
    quality     TEXT NOT NULL DEFAULT 'ok' CHECK (quality IN ('ok','rejected')),
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_session ON readings(session_id, vital, reading_id);

-- The classified result for a session: the vitals used, the tier, and the model that produced it.
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id    TEXT PRIMARY KEY,          -- also used as the MQTT message_id
    session_id       TEXT NOT NULL REFERENCES sessions(session_id),
    spo2             REAL NOT NULL,
    systolic         REAL NOT NULL,
    diastolic        REAL,
    heart_rate       REAL NOT NULL,
    temperature      REAL NOT NULL,
    respiratory_rate REAL NOT NULL,
    height_cm        REAL,                      -- informational, not a model input
    weight_kg        REAL,
    bmi              REAL,
    tier             TEXT NOT NULL,
    tier_rank        INTEGER NOT NULL,          -- Normal 0, Low 1, Medium 2, High 3
    probabilities    TEXT NOT NULL,             -- JSON
    in_model_range   INTEGER NOT NULL,          -- 0 if any input is outside the training range
    critical_flags   TEXT NOT NULL,             -- JSON list
    model_version    TEXT NOT NULL,
    captured_at      TEXT NOT NULL,
    quality          TEXT NOT NULL DEFAULT '{}'   -- JSON: RR level, which heart rate was used, ...
);

-- Messages waiting to be sent to the backend (the store-and-forward buffer).
CREATE TABLE IF NOT EXISTS outbox (
    message_id    TEXT PRIMARY KEY REFERENCES assessments(assessment_id),
    topic         TEXT NOT NULL,
    payload       TEXT NOT NULL,                -- JSON sent over MQTT
    priority      INTEGER NOT NULL,             -- same as tier_rank
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','synced')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at    TEXT NOT NULL,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, priority DESC, created_at);

-- ===== Field test log (added for the supervised field test) =====
-- When a result is not trusted and the patient is asked to measure again, the scans before this moment are ignored.
CREATE TABLE IF NOT EXISTS retake_marks (
    external_session_id TEXT NOT NULL,
    marked_at           TEXT NOT NULL,
    max_rowid           INTEGER NOT NULL          -- scans up to this row (in the scans table) no longer count
);
CREATE INDEX IF NOT EXISTS idx_retake_marks ON retake_marks(external_session_id, marked_at);

-- One row per classification (flags are display only).
CREATE TABLE IF NOT EXISTS field_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    message_id       TEXT,                     -- sent to the backend
    attempt_no       INTEGER NOT NULL,
    captured_at      TEXT NOT NULL,
    kiosk_id         TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    triage_status    TEXT,                     -- status text of triage_config.json (PLACEHOLDER until approved)
    spo2 REAL, systolic REAL, diastolic REAL, heart_rate REAL, temperature REAL, respiratory_rate REAL,
    height_cm REAL, weight_kg REAL, bmi REAL,
    p_normal REAL, p_low REAL, p_medium REAL, p_high REAL,
    tier             TEXT,
    tier_rank        INTEGER,
    confidence       REAL,
    alert_level      TEXT NOT NULL CHECK (alert_level IN ('OK','LOW_CONFIDENCE')),
    reason           TEXT,
    check_flag       INTEGER NOT NULL DEFAULT 0,  -- model says Normal/Low while a vital is critical
    critical_flags   TEXT NOT NULL,            -- JSON list
    in_model_range   INTEGER,
    tie_break_rule   TEXT,
    rr_level         TEXT,
    hr_source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_field_log_session ON field_log(session_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_field_log_message ON field_log(message_id);

-- What the clinic staff decided for the same visit: the field-test ground truth.
CREATE TABLE IF NOT EXISTS staff_feedback (
    feedback_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    staff_tier   TEXT NOT NULL CHECK (staff_tier IN ('Normal','Low','Medium','High')),
    seen_at      TEXT,                         -- when the patient was actually seen
    override     INTEGER NOT NULL,             -- 1 if the staff tier differs from the kiosk tier
    note         TEXT,
    staff_code   TEXT                          -- an initials or code, not a name
);
CREATE INDEX IF NOT EXISTS idx_staff_feedback_msg ON staff_feedback(message_id);

-- The queue as it stood each time a visit was added: both tie-break orders, so we can count how often they differ.
CREATE TABLE IF NOT EXISTS queue_snapshots (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at          TEXT NOT NULL,
    active_rule       TEXT NOT NULL,
    n_queue           INTEGER NOT NULL,
    n_low_confidence          INTEGER NOT NULL,
    order_probability TEXT NOT NULL,           -- JSON list of message ids
    order_wait        TEXT NOT NULL,
    differs           INTEGER NOT NULL,
    order_critical_first TEXT,                 -- order under the active tie-break with critical_first forced on
    differs_critical  INTEGER                  -- 1 if that order differs from the same rule with critical_first off
);
