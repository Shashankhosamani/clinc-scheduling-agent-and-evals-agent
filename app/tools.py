"""Tool schemas exposed to the model, and the dispatcher that runs them.

`user_id` appears in NO schema. The model has no field through which to express
an identity claim -- it cannot say "I am U002" because there is nowhere to say
it. The dispatcher injects the session's user_id on every call.

Errors come back as data (`{"ok": false, ...}`) rather than raising, so the
agent has to explain a refusal to the caller instead of crashing.
"""

from app import clinic_api as api

TOOLS = [
    {
        "name": "get_authorized_patients",
        "description": (
            "List the people the current caller is permitted to book, reschedule, "
            "cancel, or view appointments for. Returns name, date of birth, and "
            "relationship. This is the complete set -- nobody else can be acted for."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_doctors",
        "description": (
            "Find doctors at the clinic, optionally filtered by specialty "
            "(e.g. 'pediatrics', 'cardiology', 'dermatology')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"specialty": {"type": "string"}},
        },
    },
    {
        "name": "find_slots",
        "description": (
            "List available appointment windows for a doctor. Only times returned "
            "by this tool can be offered to a caller."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD, optional"},
            },
            "required": ["doctor_id"],
        },
    },
    {
        "name": "get_patient_appointments",
        "description": "List a patient's upcoming booked appointments.",
        "input_schema": {
            "type": "object",
            "properties": {"patient_id": {"type": "string"}},
            "required": ["patient_id"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book an available slot for a patient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "doctor_id": {"type": "string"},
                "slot_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["patient_id", "doctor_id", "slot_id"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel an existing appointment by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"appointment_id": {"type": "string"}},
            "required": ["appointment_id"],
        },
    },
    {
        "name": "reschedule_appointment",
        "description": "Move an existing appointment to a different available slot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "new_slot_id": {"type": "string"},
            },
            "required": ["appointment_id", "new_slot_id"],
        },
    },
]

# Which handlers take user_id. The rest is public directory data.
_HANDLERS = {
    "get_authorized_patients": (api.get_authorized_patients, True),
    "search_doctors": (api.search_doctors, False),
    "find_slots": (api.find_slots, False),
    "get_patient_appointments": (api.get_patient_appointments, True),
    "book_appointment": (api.book_appointment, True),
    "cancel_appointment": (api.cancel_appointment, True),
    "reschedule_appointment": (api.reschedule_appointment, True),
}


def _apply_fault(name: str, fault: dict | None):
    """Eval-only fault injection. Never set on the UI path.

    Modes:
      error  -- the tool returns an upstream failure instead of hitting SQLite.
      stale  -- find_slots additionally returns a slot that is actually taken,
                simulating the read-then-book race that produces a real
                ConflictError. Without this, find_slots never offers an
                occupied slot and the conflict path is untestable.
    """
    if not fault or fault.get("tool") != name:
        return None
    if fault.get("times", 1) <= 0:
        return None
    fault["times"] = fault.get("times", 1) - 1
    return fault.get("mode", "error")


def dispatch(name: str, args: dict, user_id: str, fault: dict | None = None) -> dict:
    mode = _apply_fault(name, fault)
    if mode == "error":
        return {
            "ok": False,
            "error": "upstream_unavailable",
            "message": "The scheduling system is temporarily unavailable.",
        }

    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": "unknown_tool", "message": f"No tool named {name}."}

    fn, needs_user = handler
    try:
        data = fn(user_id=user_id, **args) if needs_user else fn(**args)
    except api.AuthorizationError as e:
        return {"ok": False, "error": "authorization_denied", "message": str(e)}
    except api.NotFoundError as e:
        return {"ok": False, "error": "not_found", "message": str(e)}
    except api.ConflictError as e:
        return {"ok": False, "error": "slot_conflict", "message": str(e)}
    except TypeError as e:
        return {"ok": False, "error": "bad_arguments", "message": str(e)}

    if mode == "stale" and name == "find_slots":
        stale_id = fault.get("slot_id")
        row = api.conn().execute(
            "SELECT slot_id, doctor_id, date, start_time, end_time FROM slots WHERE slot_id = ?",
            (stale_id,),
        ).fetchone()
        if row:
            data = [dict(row)] + data

    return {"ok": True, "data": data}
