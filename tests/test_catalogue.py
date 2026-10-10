"""The catalogue's job is to fail at import time and at boot, never at 3am.
So these are mostly tests that registering a bad action raises.

Each test gets a clean REGISTRY: the decorator mutates module state, and a
test that passes only because of what ran before it isn't a test.
"""

import pytest

from bounds.catalogue import (
    REGISTRY,
    Action,
    BoundaryConfigError,
    Param,
    action,
    validate_boundary,
    validate_params,
    verify,
)

SEND_EMAIL = {
    "to": Param("list[str]", feeds="max_recipients"),
    "subject": Param("str", required=False),
}
SEND_EMAIL_DESC = "Send an email via the workspace Gmail account."


@pytest.fixture(autouse=True)
def clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def register_send_email():
    @action(
        "send_email", connector="gmail", description=SEND_EMAIL_DESC, params=SEND_EMAIL
    )
    def send_email(secret, to, subject=None):
        return {"sent": len(to)}

    return send_email


# --- registration -------------------------------------------------------


def test_registers_an_action():
    register_send_email()
    assert REGISTRY["send_email"].connector == "gmail"


def test_rejects_a_duplicate_name():
    register_send_email()
    with pytest.raises(BoundaryConfigError, match="duplicate"):
        register_send_email()


def test_rejects_a_non_callable_reconciler():
    with pytest.raises(BoundaryConfigError, match="reconcile must be callable"):

        @action("x", connector="c", description="d", params={}, reconcile="not-a-fn")
        def x(secret):
            pass


def test_rejects_an_unknown_param_type():
    with pytest.raises(BoundaryConfigError, match="unknown type"):

        @action("x", connector="c", description="d", params={"a": Param("int64")})
        def x(secret, a):
            pass


def test_rejects_feeding_a_limit_that_is_not_a_param_limit():
    # max_per_day counts log rows, not params. A param claiming to feed it
    # would be silently ignored by check().
    with pytest.raises(BoundaryConfigError, match="not a param limit"):

        @action("x", connector="c", description="d", params={"a": Param("number", feeds="max_per_day")})
        def x(secret, a):
            pass


def test_rejects_feeding_a_limit_from_an_unmeasurable_type():
    with pytest.raises(BoundaryConfigError, match="cannot feed"):

        @action("x", connector="c", description="d", params={"a": Param("str", feeds="max_spend_usd")})
        def x(secret, a):
            pass


def test_rejects_a_signature_that_does_not_match_the_declared_params():
    # The schema would describe something other than what runs — and the schema
    # is what every limit reads.
    with pytest.raises(BoundaryConfigError, match="signature"):

        @action("x", connector="c", description="d", params={"to": Param("list[str]")})
        def x(secret, recipients):
            pass


# --- params validation --------------------------------------------------


def test_validate_params_accepts_a_matching_shape():
    register_send_email()
    schema = REGISTRY["send_email"].schema()
    assert validate_params(schema, {"to": ["a@b.com"], "subject": "hi"}) is None


def test_validate_params_allows_an_omitted_optional():
    register_send_email()
    schema = REGISTRY["send_email"].schema()
    assert validate_params(schema, {"to": ["a@b.com"]}) is None


def test_validate_params_rejects_a_bool_as_a_number():
    # bool is an int in Python; True is not a quantity.
    schema = {"amount_usd": {"type": "number", "required": True, "feeds": None}}
    assert "expected number" in validate_params(schema, {"amount_usd": True})


# --- authoring ----------------------------------------------------------


def test_validate_boundary_rejects_an_uncatalogued_action():
    register_send_email()
    with pytest.raises(BoundaryConfigError, match="unknown action"):
        validate_boundary({}, ["send_email", "delete_file"])


def test_validate_boundary_rejects_an_unenforceable_limit():
    register_send_email()
    with pytest.raises(BoundaryConfigError, match="no param feeding it"):
        validate_boundary({"max_spend_usd": 100}, ["send_email"])


def test_validate_boundary_accepts_an_enforceable_one():
    register_send_email()
    validate_boundary({"max_recipients": 1, "max_per_day": 20}, ["send_email"])


# --- drift --------------------------------------------------------------


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self.rows


def db_row(name="send_email", connector="gmail", schema=None):
    return {
        "name": name,
        "connector": connector,
        "description": SEND_EMAIL_DESC,
        "params_schema": schema
        if schema is not None
        else Action(
            "send_email", "gmail", SEND_EMAIL_DESC, SEND_EMAIL, lambda: None
        ).schema(),
    }


def test_verify_passes_when_the_copy_matches():
    register_send_email()
    verify(FakeConn([db_row()]))


def test_verify_catches_an_action_with_no_implementation():
    register_send_email()
    conn = FakeConn([db_row(), db_row(name="wire_transfer", connector="bank")])
    with pytest.raises(BoundaryConfigError, match="no implementation"):
        verify(conn)


def test_verify_catches_an_implementation_missing_from_the_catalogue():
    register_send_email()
    with pytest.raises(BoundaryConfigError, match="run sync"):
        verify(FakeConn([]))


def test_verify_catches_a_widened_schema():
    # The dangerous drift: code now takes a param the catalogue never declared,
    # so check() would reject it as unknown — or, if the schema were the stale
    # one in the DB, cap a field nobody sends.
    register_send_email()
    stale = {"to": {"type": "list[str]", "required": True, "feeds": None}}
    with pytest.raises(BoundaryConfigError, match="disagree"):
        verify(FakeConn([db_row(schema=stale)]))
