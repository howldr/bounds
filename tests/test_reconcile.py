"""reconcile() closes unknown outcomes only when the downstream can
actually say. Everything else stays unknown — an unknown that stays
unknown is more honest than a guess.
"""

import pytest

from bounds.catalogue import REGISTRY, Param, action
from bounds.executor import reconcile

CRASH_ROW = {
    "id": 42,
    "agent": "invoice-followup",
    "boundary_version": 1,
    "action_requested": "send_email",
    "action_params": {"to": ["a@b.com"]},
}


class FakeConn:
    def __init__(self, rows):
        self.rows, self.updates = rows, []

    def execute(self, sql, params=None):
        if "UPDATE action_log" in sql:
            self.updates.append(params)
        elif "FROM boundary" in sql:
            self._row = {"credential_ref": "demo-send-only"}
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self._row


@pytest.fixture(autouse=True)
def clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def register(reconcile_fn):
    @action(
        "send_email", connector="demo", description="d",
        params={"to": Param("list[str]", feeds="max_recipients")},
        reconcile=reconcile_fn,
    )
    def send_email(secret, to):
        pass


def test_a_reconciler_that_can_answer_closes_the_row():
    seen = {}

    def rec(secret, params):
        seen.update(secret=secret, params=params)
        return ("success", {"note": "ledger says delivered"})

    register(rec)
    conn = FakeConn([CRASH_ROW])
    report = reconcile(conn, lambda ref: f"secret-for-{ref}")

    assert report == [{"log_id": 42, "action": "send_email", "state": "success"}]
    assert seen["secret"] == "secret-for-demo-send-only"
    ((outcome, result_json, row_id),) = conn.updates
    assert outcome == "success" and row_id == 42
    assert result_json.obj["reconciled"] is True


def test_no_reconciler_leaves_the_row_unknown():
    register(None)
    conn = FakeConn([CRASH_ROW])
    report = reconcile(conn, lambda ref: "s")
    assert report[0]["state"] == "unresolved" and "no reconciler" in report[0]["why"]
    assert conn.updates == []


def test_a_reconciler_that_cannot_say_leaves_the_row_unknown():
    register(lambda secret, params: None)
    conn = FakeConn([CRASH_ROW])
    report = reconcile(conn, lambda ref: "s")
    assert report[0]["state"] == "unresolved" and "cannot say" in report[0]["why"]
    assert conn.updates == []


def test_a_raising_reconciler_is_an_unresolved_row_not_a_crash():
    def rec(secret, params):
        raise ConnectionError("downstream is down")

    register(rec)
    conn = FakeConn([CRASH_ROW])
    report = reconcile(conn, lambda ref: "s")
    assert report[0]["state"] == "unresolved" and "ConnectionError" in report[0]["why"]
    assert conn.updates == []
