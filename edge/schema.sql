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
