"""The generated tool list must be exactly the boundary, no more — an action
outside actions_allowed must never be offered, because a tool the model can
see is a tool it will eventually try.
"""

import pytest

from bounds.catalogue import BoundaryConfigError
from bounds.tools import tools_for

SEND_EMAIL = {
    "name": "send_email",
    "description": "Send an email via the workspace Gmail account.",
    "params_schema": {
        "to": {
            "type": "list[str]",
            "required": True,
            "feeds": "max_recipients",
            "desc": "Recipient email addresses.",
        },
        "subject": {"type": "str", "required": False, "feeds": None, "desc": None},
    },
}
CREATE_INVOICE = {
    "name": "create_invoice",
    "description": "Create a draft invoice in Xero.",
    "params_schema": {
        "amount_usd": {
            "type": "number",
            "required": True,
            "feeds": "max_spend_usd",
            "desc": None,
        },
    },
}
BOUNDARY = {
    "agent": "invoice-followup",
    "version": 1,
    "actions_allowed": ["send_email"],
    "limits": {"max_recipients": 1, "max_per_day": 20},
    "data_scope": ["invoices.read"],
    "model_tier": "local",
    "credential_ref": "gmail-send-only",
}


class FakeConn:
    def __init__(self, boundary=BOUNDARY, actions=(SEND_EMAIL, CREATE_INVOICE)):
        self.boundary = boundary
        self.actions = {a["name"]: a for a in actions}

    def execute(self, sql, params=None):
        if "FROM boundary" in sql:
            self._row = self.boundary
        elif "FROM action" in sql:
            self._row = self.actions.get(params[0])
        return self

    def fetchone(self):
        return self._row


def test_offers_exactly_the_boundary_and_nothing_else():
    tools = tools_for(FakeConn(), "invoice-followup")
    assert [t["name"] for t in tools] == ["send_email"]
    # create_invoice is catalogued and implemented — and not offered, because
    # this agent's boundary doesn't grant it.


def test_no_boundary_offers_nothing():
    assert tools_for(FakeConn(boundary=None), "unknown-agent") == []


def test_schema_matches_what_check_will_enforce():
    (tool,) = tools_for(FakeConn(), "invoice-followup")
    assert tool["input_schema"] == {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recipient email addresses.",
            },
        },
        "required": ["to"],
        "additionalProperties": False,
    }


def test_description_states_the_limits():
    (tool,) = tools_for(FakeConn(), "invoice-followup")
    assert tool["description"].startswith("Send an email")
    assert "max_recipients=1 (summed across: to)" in tool["description"]
    assert "max_per_day=20" in tool["description"]


def test_hours_render_as_a_range():
    b = BOUNDARY | {"limits": {"hours_allowed": [9, 17]}}
    (tool,) = tools_for(FakeConn(boundary=b), "invoice-followup")
    assert "hours_allowed=9:00–17:00" in tool["description"]


def test_no_limits_means_no_limits_note():
    b = BOUNDARY | {"limits": {}}
    (tool,) = tools_for(FakeConn(boundary=b), "invoice-followup")
    assert tool["description"] == SEND_EMAIL["description"]


def test_multiple_grants_come_out_sorted():
    # Deterministic order: a tool list that varies between runs invalidates
    # any prompt cache in front of it.
    b = BOUNDARY | {"actions_allowed": ["send_email", "create_invoice"]}
    tools = tools_for(FakeConn(boundary=b), "invoice-followup")
    assert [t["name"] for t in tools] == ["create_invoice", "send_email"]


def test_uncatalogued_grant_is_loud():
    # tools_for is not the gate; a malformed boundary here should raise,
    # not quietly offer a smaller menu.
    b = BOUNDARY | {"actions_allowed": ["send_email", "wire_transfer"]}
    with pytest.raises(BoundaryConfigError, match="wire_transfer"):
        tools_for(FakeConn(boundary=b), "invoice-followup")
