"""The authorization invariant is the whole submission. It gets a real test.

Run: python -m tests.test_clinic_api
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clinic_api as api
from app import db


def setup():
    conn = db.connect(":memory:")
    api.set_connection(conn)
    db.reset_and_seed(conn)
    return conn


def test_authorized_list_is_scoped():
    setup()
    ids = {p["patient_id"] for p in api.get_authorized_patients("U001")}
    assert ids == {"P001", "P002", "P003", "P004"}, ids
    assert "P091" not in ids
    assert {p["patient_id"] for p in api.get_authorized_patients("U002")} == {"P005"}


def test_book_for_unauthorized_patient_is_denied():
    setup()
    try:
        api.book_appointment("U001", "P091", "D03", "S0014")
        raise AssertionError("IDOR: booked an unauthorized patient")
    except api.AuthorizationError:
        pass
    # and nothing was written
    rows = api.conn().execute(
        "SELECT COUNT(*) c FROM appointments WHERE patient_id='P091' AND status='booked'"
    ).fetchone()
    assert rows["c"] == 1, "pre-existing appointment count changed"


def test_reading_unauthorized_patient_is_denied():
    setup()
    try:
        api.get_patient_appointments("U001", "P091")
        raise AssertionError("disclosed an unauthorized patient's records")
    except api.AuthorizationError:
        pass


def test_denial_is_audited():
    setup()
    try:
        api.book_appointment("U001", "P091", "D03", "S0014")
    except api.AuthorizationError:
        pass
    row = api.conn().execute(
        "SELECT * FROM access_log WHERE decision='denied' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "denial was not logged"
    assert row["user_id"] == "U001" and row["patient_id"] == "P091"


def test_double_booking_is_structurally_blocked():
    setup()
    # S0007 is held by A0002 in the seed data.
    try:
        api.book_appointment("U001", "P002", "D02", "S0007")
        raise AssertionError("double-booked an occupied slot")
    except api.ConflictError:
        pass


def test_booked_slot_never_offered():
    setup()
    free = {s["slot_id"] for s in api.find_slots("D02")}
    assert "S0007" not in free, "find_slots offered an occupied slot"


def test_cancel_authorizes_against_patient_not_booker():
    setup()
    # A0003 is Meera's (P091), booked by U002. U001 must not be able to cancel it.
    try:
        api.cancel_appointment("U001", "A0003")
        raise AssertionError("cancelled another user's patient's appointment")
    except api.AuthorizationError:
        pass
    # U001 CAN cancel Rajesh's (P003, authorized as 'parent')
    assert api.cancel_appointment("U001", "A0001")["status"] == "cancelled"


def test_cancelling_frees_the_slot():
    setup()
    api.cancel_appointment("U001", "A0001")
    assert "S0001" in {s["slot_id"] for s in api.find_slots("D01")}


def test_happy_path_book():
    setup()
    result = api.book_appointment("U001", "P002", "D02", "S0008", "fever")
    assert result["status"] == "booked"
    appts = api.get_patient_appointments("U001", "P002")
    assert len(appts) == 1 and appts[0]["doctor_name"] == "Dr. Rao"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
