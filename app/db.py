"""SQLite schema and seeding for the mock clinic.

Single clinic, single location, one timezone (IST). Slot times are wall-clock
strings, not instants.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "clinic.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS access_log;
DROP TABLE IF EXISTS appointments;
DROP TABLE IF EXISTS slots;
DROP TABLE IF EXISTS doctors;
DROP TABLE IF EXISTS authorizations;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS users;

-- The authenticated account talking to the agent. NOT necessarily a patient.
CREATE TABLE users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    phone        TEXT NOT NULL
);

CREATE TABLE patients (
    patient_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    dob        TEXT NOT NULL,
    notes      TEXT DEFAULT ''
);

-- A row here means: this user may act on behalf of this patient.
-- Pre-established consent on file, NOT self-declared during a conversation.
CREATE TABLE authorizations (
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    patient_id   TEXT NOT NULL REFERENCES patients(patient_id),
    relationship TEXT NOT NULL,
    PRIMARY KEY (user_id, patient_id)
);

CREATE TABLE doctors (
    doctor_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    specialty TEXT NOT NULL,
    notes     TEXT DEFAULT ''
);

-- Fixed windows set by the clinic. Callers pick from these; no free-form times.
CREATE TABLE slots (
    slot_id    TEXT PRIMARY KEY,
    doctor_id  TEXT NOT NULL REFERENCES doctors(doctor_id),
    date       TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL
);

CREATE TABLE appointments (
    appointment_id TEXT PRIMARY KEY,
    patient_id     TEXT NOT NULL REFERENCES patients(patient_id),
    booked_by      TEXT NOT NULL REFERENCES users(user_id),
    doctor_id      TEXT NOT NULL REFERENCES doctors(doctor_id),
    slot_id        TEXT NOT NULL REFERENCES slots(slot_id),
    reason         TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'booked',
    created_at     TEXT NOT NULL
);

-- Double-booking is structurally impossible, not merely discouraged by a prompt.
CREATE UNIQUE INDEX idx_active_slot
    ON appointments(slot_id) WHERE status = 'booked';

-- Every patient-scoped attempt, allowed or denied. The denied rows are the
-- security signal a real clinic would alert on.
CREATE TABLE access_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    patient_id TEXT,
    action     TEXT NOT NULL,
    decision   TEXT NOT NULL,
    reason     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def reset_and_seed(conn: sqlite3.Connection) -> None:
    """Wipe and reseed. Called before every eval scenario so runs are comparable."""
    from data import seed

    conn.executescript(SCHEMA)
    seed.load(conn)
    conn.commit()
