import pytest

from ops_chief import db, policy


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.seed(c)
    return c


def test_email_always_needs_a_person(conn):
    d = policy.decide(conn, "send_email", {"to": "a@b.example", "body": "hi"})
    assert d.outcome == "approval"


def test_email_without_recipient_is_blocked(conn):
    assert policy.decide(conn, "send_email", {"to": "", "body": "hi"}).outcome == "blocked"


def test_matching_small_invoice_waits_for_approval(conn):
    assert policy.decide(conn, "approve_payment", {"invoice_id": "INV-9001"}).outcome == "approval"


def test_invoice_over_po_tolerance_is_blocked(conn):
    d = policy.decide(conn, "approve_payment", {"invoice_id": "INV-9002"})
    assert d.outcome == "blocked" and "does not match" in d.reason


def test_invoice_without_po_is_blocked(conn):
    assert policy.decide(conn, "approve_payment", {"invoice_id": "INV-9004"}).outcome == "blocked"


def test_large_matching_invoice_needs_owner(conn):
    conn.execute("UPDATE purchase_orders SET amount = 19950 WHERE po_number = 'PO-5501'")
    d = policy.decide(conn, "approve_payment", {"invoice_id": "INV-9002"})
    assert d.outcome == "approval" and "owner" in d.reason


def test_marking_stalled_is_automatic_but_closing_is_not(conn):
    assert policy.decide(conn, "update_task_status", {"task_id": "T-201", "status": "stalled"}).outcome == "auto"
    assert policy.decide(conn, "update_task_status", {"task_id": "T-201", "status": "done"}).outcome == "approval"


def test_unknown_action_is_blocked(conn):
    assert policy.decide(conn, "wire_money", {}).outcome == "blocked"


def test_resolve_only_pending(conn):
    r = policy.submit(conn, "run-x", "followup", "send_email", {"to": "a@b.example", "body": "hi"})
    assert "approved" in policy.resolve(conn, r["action_id"], True, "owner")
    assert "not pending" in policy.resolve(conn, r["action_id"], False, "owner")
