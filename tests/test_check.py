"""Deny paths first. An allow test that passes proves little; a deny test that
fails means the gate is open.

FakeConn stands in for the DB — check() only ever reads, so a dict-returning
stub is honest here. dispatch() is not tested this way; it writes, and a fake
that pretends to enforce the idempotency constraint would test nothing. That
needs a real Postgres.
"""

import pytest

from bounds.check import BoundaryConfigError, check, validate_limits
from bounds.models import ActionRequest

BOUNDARY = {
    "version": 1,
    "data_scope": ["invoices.read", "contacts.read"],
    "actions_allowed": ["send_email"],
    "limits": {"max_recipients": 1, "max_per_day": 20},
    "model_tier": "local",
    "credential_ref": "gmail-send-only",
    "approval_required": False,
}
CREDENTIAL = {
    "ref": "gmail-send-only",
    "connector": "gmail",
    "scope": ["send_email"],
    "expires_at": None,
}
ACTION = {
    "name": "send_email",
    "connector": "gmail",
    "params_schema": {
        "to": {"type": "list[str]", "required": True, "feeds": "max_recipients"},
        "subject": {"type": "str", "required": False, "feeds": None},
        "body": {"type": "str", "required": False, "feeds": None},
    },
}


class FakeConn:
    """Returns boundary/action/credential/count based on which query is asked."""

    def __init__(
        self, boundary=BOUNDARY, credential=CREDENTIAL, action=ACTION, used_today=0
    ):
        self.boundary, self.credential = boundary, credential
        self.action, self.used_today = action, used_today

    def execute(self, sql, params=None):
        # count(*) first: its FROM clause is action_log, which contains 'action'.
        if "count(*)" in sql:
            self._row = {"n": self.used_today}
        elif "FROM boundary" in sql:
            self._row = self.boundary
        elif "FROM credential" in sql:
            self._row = self.credential
        elif "FROM action" in sql:
            self._row = self.action
        return self

    def fetchone(self):
        return self._row


def req(**over):
    base = dict(
        idempotency_key="k1", trigger_type="event", trigger_source="test",
        requested_by="operator", agent="invoice-followup", action="send_email",
        params={"to": ["a@b.com"]}, data_touched=["invoices.read"],
        model_tier_used="local",
    )
    return ActionRequest(**(base | over))


def test_allows_a_request_inside_every_limit():
    d = check(FakeConn(), req())
    assert d.allow and d.credential_ref == "gmail-send-only"


def test_denies_when_no_boundary_exists():
    d = check(FakeConn(boundary=None), req())
    assert not d.allow and "no active boundary" in d.reason


def test_denies_action_outside_scope():
    d = check(FakeConn(), req(action="delete_file"))
    assert not d.allow and "actions_allowed" in d.reason


def test_denies_data_outside_scope():
    d = check(FakeConn(), req(data_touched=["payroll.read"]))
    assert not d.allow and "data scope violation" in d.reason


def test_denies_cloud_model_under_local_boundary():
    d = check(FakeConn(), req(model_tier_used="cloud"))
    assert not d.allow and "local model tier" in d.reason


def test_denies_too_many_recipients():
    d = check(FakeConn(), req(params={"to": ["a@b.com", "c@d.com"]}))
    assert not d.allow and "max_recipients" in d.reason


def test_denies_at_the_daily_cap_not_past_it():
    # 20 used against a cap of 20 must deny — off-by-one here means 21 sent.
    d = check(FakeConn(used_today=20), req())
    assert not d.allow and "max_per_day" in d.reason


def test_denies_credential_broader_than_boundary():
    wide = CREDENTIAL | {"scope": ["send_email", "delete_mail"]}
    d = check(FakeConn(credential=wide), req())
    assert not d.allow and "credential scope exceeds boundary" in d.reason


def test_denies_unresolvable_credential():
    d = check(FakeConn(credential=None), req())
    assert not d.allow and "does not resolve" in d.reason


def test_holds_when_the_boundary_demands_approval():
    d = check(FakeConn(boundary=BOUNDARY | {"approval_required": True}), req())
    assert not d.allow and d.hold and "approval required" in d.reason
    assert d.credential_ref is None  # nothing to hold it *with* until a human says so


def test_approved_satisfies_the_approval_gate_only():
    b = BOUNDARY | {"approval_required": True}
    d = check(FakeConn(boundary=b), req(), approved=True)
    assert d.allow and not d.hold
    # ...and an approval does not bypass the other limits:
    d = check(FakeConn(boundary=b), req(params={"to": ["a@b.com", "c@d.com"]}),
              approved=True)
    assert not d.allow and not d.hold and "max_recipients" in d.reason


def test_a_hold_is_always_a_would_be_allow():
    # Requests that would deny still deny — a human's attention is only
    # asked for proposals that cleared every other gate.
    b = BOUNDARY | {"approval_required": True}
    d = check(FakeConn(boundary=b), req(action="delete_file"))
    assert not d.allow and not d.hold


def test_denies_an_expired_credential():
    # expires_at comes back from a timestamptz column, so it is aware.
    # The default `now` must also be aware, or this raises instead of denying.
    from datetime import UTC, datetime

    expired = CREDENTIAL | {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)}
    d = check(FakeConn(credential=expired), req())
    assert not d.allow and "credential expired" in d.reason


def test_denies_boundary_with_a_typo_in_limits():
    # `max_recipient` (singular) must not silently disable the limit.
    typo = BOUNDARY | {"limits": {"max_recipient": 1}}
    d = check(FakeConn(boundary=typo), req(params={"to": ["a@b.com", "c@d.com"]}))
    assert not d.allow and "unknown limit keys" in d.reason


def test_validate_limits_rejects_typos_at_authoring_time():
    with pytest.raises(BoundaryConfigError):
        validate_limits({"max_recipient": 1})


# --- the catalogue standing behind the limits ---------------------------
# The bug these exist for: a limit reads its param by name. If the params are
# shaped differently, `params.get("to", [])` is empty, and empty is inside
# every limit. The gate returns allow on a request it never understood.


def test_denies_params_the_action_does_not_declare():
    d = check(FakeConn(), req(params={"to": ["a@b.com"], "bcc": ["x@y.com"]}))
    assert not d.allow and "unknown params ['bcc']" in d.reason


def test_denies_a_missing_required_param():
    # Previously: len(params.get("to", [])) == 0, which is <= max_recipients.
    d = check(FakeConn(), req(params={"subject": "hi"}))
    assert not d.allow and "missing required param 'to'" in d.reason


def test_denies_a_param_of_the_wrong_type():
    # 'to' as a bare string: len() would have been 7, denying for the wrong
    # reason. A wrong-shaped request should say so.
    d = check(FakeConn(), req(params={"to": "a@b.com"}))
    assert not d.allow and "expected list[str]" in d.reason


def test_denies_a_limit_no_param_feeds():
    # send_email has no money param, so max_spend_usd cannot be enforced on it.
    # Silently ignoring it would grant unlimited spend on a boundary whose
    # author believed they had capped it.
    b = BOUNDARY | {"limits": {"max_spend_usd": 100}}
    d = check(FakeConn(boundary=b), req())
    assert not d.allow and "no param feeding it" in d.reason


def test_sums_every_param_feeding_a_limit():
    # cc counts against max_recipients too, rather than getting its own
    # allowance. One recipient in 'to' + one in 'cc' is two against a cap of 1.
    schema = ACTION["params_schema"] | {
        "cc": {"type": "list[str]", "required": False, "feeds": "max_recipients"}
    }
    conn = FakeConn(action=ACTION | {"params_schema": schema})
    d = check(conn, req(params={"to": ["a@b.com"], "cc": ["c@d.com"]}))
    assert not d.allow and "max_recipients 2 > 1" in d.reason


def test_denies_an_action_missing_from_the_catalogue():
    d = check(FakeConn(action=None), req())
    assert not d.allow and "not in the catalogue" in d.reason


class RecordingConn(FakeConn):
    """FakeConn that keeps the params it was asked with.

    Tenant isolation is not a value check() computes and returns — it is a
    clause in the queries it asks. The only honest assertion at this level is
    that the tenant reached the SQL; that it *isolates* is the database's job,
    and is proven against real Postgres in the live smoke.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return super().execute(sql, params)

    def query_for(self, needle):
        return next(p for sql, p in self.calls if needle in sql)


def test_the_boundary_lookup_is_scoped_to_the_tenant():
    # Without this clause, acme's agent name resolves to whichever boundary
    # happens to be active anywhere — the cross-tenant grant in one query.
    conn = RecordingConn()
    check(conn, req(tenant="acme"))
    assert conn.query_for("FROM boundary")[0] == "acme"


def test_the_daily_count_is_scoped_to_the_tenant():
    # A shared count means one tenant's sends exhaust another's allowance,
    # and a busy neighbour becomes a denial of service.
    conn = RecordingConn()
    check(conn, req(tenant="acme"))
    assert conn.query_for("count(*)")[0] == "acme"


def test_a_request_without_a_tenant_lands_in_default():
    # Single-tenant deployments never name one; they must not fall outside
    # every boundary as a result.
    conn = RecordingConn()
    d = check(conn, req())
    assert d.allow and conn.query_for("FROM boundary")[0] == "default"
