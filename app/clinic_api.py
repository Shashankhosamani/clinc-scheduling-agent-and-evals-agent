"""Mock clinic backend.

THE INVARIANT: the LLM never decides authorization. It may request any
patient_id; `_assert_authorized` decides whether that request is legal. No
prompt wording -- hostile, injected, or merely sloppy -- can reach this code.

`user_id` is the first parameter of every patient-scoped function and is
injected by the tool dispatcher from session state, never by the model.

In production this would sit behind HTTP with its own auth and the agent would
be a client. It is in-process here because the security property is identical
(the check runs in code the model cannot reach) and the plumbing would add
nothing to what this project is demonstrating.
"""

import sqlite3
import threading
from datetime import datetime

from app import db


class AuthorizationError(Exception):
    """The caller is not permitted to act for this patient."""


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    """The requested slot is no longer available."""


# Thread-local, not a module global. SQLite connections cannot be shared across
# threads, and the eval runner runs scenarios concurrently -- each worker thread
# gets its own connection to its own database file, so one scenario's bookings
# can never leak into another's assertions.
_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = db.connect()
    return c


def set_connection(c: sqlite3.Connection) -> None:
    """Point this thread at a specific database (used by the eval runner)."""
    _local.conn = c


def _log(user_id: str, patient_id: str | None, action: str, decision: str, reason: str = "") -> None:
    conn().execute(
        "INSERT INTO access_log (user_id, patient_id, action, decision, reason, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, patient_id, action, decision, reason, datetime.now().isoformat(timespec="seconds")),
    )
    conn().commit()


def _assert_authorized(user_id: str, patient_id: str, action: str) -> None:
    """THE INVARIANT. Every patient-scoped operation calls this first.

    Audit logging lives here rather than in the callers, so a new patient-scoped
    operation cannot forget to audit -- it gets logging by virtue of being
    authorized at all.
    """
    row = conn().execute(
        "SELECT 1 FROM authorizations WHERE user_id = ? AND patient_id = ?",
        (user_id, patient_id),
    ).fetchone()

    if row is None:
        _log(user_id, patient_id, action, "denied", "no authorization on file")
        raise AuthorizationError(
            f"User {user_id} is not authorized to act for patient {patient_id}."
        )

    _log(user_id, patient_id, action, "allowed")


# --------------------------------------------------------------------------
# Patient-scoped operations
# --------------------------------------------------------------------------

def get_authorized_patients(user_id: str) -> list[dict]:
    """The only source of truth for who this caller may act for.

    Scoped to the caller by construction -- there is no code path here that can
    return a patient the user is not authorized for, so the model cannot coax
    a global patient list out of it.
    """
    rows = conn().execute(
        "SELECT p.patient_id, p.name, p.dob, p.notes, a.relationship "
        "FROM authorizations a JOIN patients p ON p.patient_id = a.patient_id "
        "WHERE a.user_id = ? ORDER BY p.patient_id",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_patient_appointments(user_id: str, patient_id: str) -> list[dict]:
    _assert_authorized(user_id, patient_id, "view")
    rows = conn().execute(
        "SELECT a.appointment_id, a.patient_id, p.name AS patient_name, "
        "       a.doctor_id, d.name AS doctor_name, d.specialty, "
        "       s.date, s.start_time, s.end_time, a.reason, a.status "
        "FROM appointments a "
        "JOIN patients p ON p.patient_id = a.patient_id "
        "JOIN doctors  d ON d.doctor_id  = a.doctor_id "
        "JOIN slots    s ON s.slot_id    = a.slot_id "
        "WHERE a.patient_id = ? AND a.status = 'booked' "
        "ORDER BY s.date, s.start_time",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def book_appointment(
    user_id: str,
    patient_id: str,
    doctor_id: str,
    slot_id: str,
    reason: str = "",
) -> dict:
    _assert_authorized(user_id, patient_id, "book")

    slot = conn().execute(
        "SELECT * FROM slots WHERE slot_id = ? AND doctor_id = ?", (slot_id, doctor_id)
    ).fetchone()
    if slot is None:
        raise NotFoundError(f"No slot {slot_id} for doctor {doctor_id}.")

    n = conn().execute("SELECT COUNT(*) AS c FROM appointments").fetchone()["c"]
    appointment_id = f"A{n + 1:04d}"

    try:
        conn().execute(
            "INSERT INTO appointments "
            "(appointment_id, patient_id, booked_by, doctor_id, slot_id, reason, status, created_at) "
            "VALUES (?,?,?,?,?,?,'booked',?)",
            (
                appointment_id,
                patient_id,
                user_id,
                doctor_id,
                slot_id,
                reason,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn().commit()
    except sqlite3.IntegrityError:
        # The partial unique index on (slot_id) WHERE status='booked' caught a
        # double-book. Structural, not a check the agent could talk past.
        conn().rollback()
        raise ConflictError(f"Slot {slot_id} has already been taken.")

    return {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "slot_id": slot_id,
        "date": slot["date"],
        "start_time": slot["start_time"],
        "end_time": slot["end_time"],
        "status": "booked",
    }


def _get_appointment(appointment_id: str) -> sqlite3.Row:
    row = conn().execute(
        "SELECT * FROM appointments WHERE appointment_id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No appointment {appointment_id}.")
    return row


def cancel_appointment(user_id: str, appointment_id: str) -> dict:
    """Authorized against the appointment's PATIENT, not against who booked it.

    Otherwise a user who lost authorization would keep the power to cancel
    appointments they created, and a co-guardian who did not create the
    appointment could not manage it. Ownership follows the patient.
    """
    appt = _get_appointment(appointment_id)
    _assert_authorized(user_id, appt["patient_id"], "cancel")

    if appt["status"] != "booked":
        raise ConflictError(f"Appointment {appointment_id} is already {appt['status']}.")

    conn().execute(
        "UPDATE appointments SET status = 'cancelled' WHERE appointment_id = ?",
        (appointment_id,),
    )
    conn().commit()
    return {"appointment_id": appointment_id, "status": "cancelled"}


def reschedule_appointment(user_id: str, appointment_id: str, new_slot_id: str) -> dict:
    appt = _get_appointment(appointment_id)
    _assert_authorized(user_id, appt["patient_id"], "reschedule")

    if appt["status"] != "booked":
        raise ConflictError(f"Appointment {appointment_id} is {appt['status']}.")

    slot = conn().execute("SELECT * FROM slots WHERE slot_id = ?", (new_slot_id,)).fetchone()
    if slot is None:
        raise NotFoundError(f"No slot {new_slot_id}.")

    try:
        conn().execute(
            "UPDATE appointments SET slot_id = ?, doctor_id = ? WHERE appointment_id = ?",
            (new_slot_id, slot["doctor_id"], appointment_id),
        )
        conn().commit()
    except sqlite3.IntegrityError:
        conn().rollback()
        raise ConflictError(f"Slot {new_slot_id} has already been taken.")

    return {
        "appointment_id": appointment_id,
        "slot_id": new_slot_id,
        "doctor_id": slot["doctor_id"],
        "date": slot["date"],
        "start_time": slot["start_time"],
        "status": "booked",
    }


# --------------------------------------------------------------------------
# Public directory data -- not patient-scoped, no authorization needed
# --------------------------------------------------------------------------

def search_doctors(specialty: str | None = None) -> list[dict]:
    """No `location` param: one clinic, one location, so a location filter would
    be a parameter the model could only ever get wrong."""
    if specialty:
        rows = conn().execute(
            "SELECT doctor_id, name, specialty, notes FROM doctors "
            "WHERE LOWER(specialty) = LOWER(?) ORDER BY doctor_id",
            (specialty,),
        ).fetchall()
    else:
        rows = conn().execute(
            "SELECT doctor_id, name, specialty, notes FROM doctors ORDER BY doctor_id"
        ).fetchall()
    return [dict(r) for r in rows]


def find_slots(doctor_id: str, date: str | None = None) -> list[dict]:
    """Free fixed windows only. A slot held by an active appointment never appears."""
    sql = (
        "SELECT s.slot_id, s.doctor_id, s.date, s.start_time, s.end_time "
        "FROM slots s "
        "LEFT JOIN appointments a ON a.slot_id = s.slot_id AND a.status = 'booked' "
        "WHERE s.doctor_id = ? AND a.appointment_id IS NULL "
    )
    params: list = [doctor_id]
    if date:
        sql += "AND s.date = ? "
        params.append(date)
    sql += "ORDER BY s.date, s.start_time"
    return [dict(r) for r in conn().execute(sql, params).fetchall()]
