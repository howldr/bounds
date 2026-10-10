"""interpret() is loop-and-translation; the gate itself is tested in
test_check. So these stub tools_for and dispatch, and test the contract that
matters here: every proposal becomes a gated ActionRequest, denies go back
as errors the model can adapt to, and nothing loops forever.
"""

from types import SimpleNamespace as NS

import pytest

import bounds.interpret as interpret_mod
from bounds.interpret import _idempotency_key, interpret
from bounds.models import ActionRequest

TOOL = {
    "name": "send_email",
    "description": "Send an email.",
    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
}


def tool_use(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


def text(t):
    return NS(type="text", text=t)


def resp(*blocks, stop_reason="tool_use"):
    return NS(content=list(blocks), stop_reason=stop_reason)


class FakeClient:
    """client.messages.create pops scripted responses; records each request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        # interpret() mutates one messages list across turns; snapshot it,
        # or every recorded request shows the final state.
        self.requests.append(kw | {"messages": list(kw["messages"])})
        return self._responses.pop(0)


@pytest.fixture
def gate(monkeypatch):
    """Stub the boundary read and the gate; record what reaches dispatch."""
    dispatched = []

    def fake_dispatch(conn, req, connectors, resolve_secret):
        dispatched.append(req)
        return {"allowed": True, "outcome": "success", "result": {}, "log_id": len(dispatched)}

    monkeypatch.setattr(interpret_mod, "tools_for", lambda conn, agent, tenant: [TOOL])
    monkeypatch.setattr(interpret_mod, "dispatch", fake_dispatch)
    return dispatched


def run(client, **over):
    kw = dict(
        agent="invoice-followup", requested_by="operator",
        prompt="chase the overdue invoices", trigger_source="repl",
        connectors={}, resolve_secret=lambda ref: "secret",
    )
    return interpret(None, client, **(kw | over))


def test_a_proposal_becomes_a_gated_request(gate):
    client = FakeClient([
        resp(tool_use("tu_1", "send_email", {"to": ["a@b.com"]})),
        resp(text("Done — one reminder sent."), stop_reason="end_turn"),
    ])
    out = run(client)

    (req,) = gate
    assert isinstance(req, ActionRequest)
    assert req.trigger_type == "nl"
    assert req.model_tier_used == "cloud"  # honest: this module calls a hosted model
    assert req.action == "send_email" and req.params == {"to": ["a@b.com"]}
    assert out["stopped"] == "end_turn" and out["text"] == "Done — one reminder sent."


def test_the_menu_offered_is_the_boundary_menu(gate):
    client = FakeClient([resp(text("nothing to do"), stop_reason="end_turn")])
    run(client)
    assert client.requests[0]["tools"] == [TOOL]


def test_a_deny_goes_back_as_an_error_result(gate, monkeypatch):
    monkeypatch.setattr(
        interpret_mod, "dispatch",
        lambda *a: {"allowed": False, "reason": "max_recipients 2 > 1", "log_id": 1},
    )
    client = FakeClient([
        resp(tool_use("tu_1", "send_email", {"to": ["a@b.com", "c@d.com"]})),
        resp(text("Cap is 1 recipient; I can send them one at a time."), stop_reason="end_turn"),
    ])
    out = run(client)

    # The deny reached the model as an error it could adapt to...
    (result,) = client.requests[1]["messages"][-1]["content"]
    assert result["is_error"] and "max_recipients" in result["content"]
    # ...and the loop continued rather than crashing or overriding.
    assert out["stopped"] == "end_turn"


def test_identical_proposals_collide_on_idempotency():
    a = _idempotency_key("repl", "send_email", {"to": ["a@b.com"], "subject": "hi"})
    b = _idempotency_key("repl", "send_email", {"subject": "hi", "to": ["a@b.com"]})
    c = _idempotency_key("repl", "send_email", {"to": ["other@b.com"]})
    assert a == b  # key order must not matter: same send, same key
    assert a != c


def test_max_turns_stops_a_model_that_never_finishes(gate):
    endless = [
        resp(tool_use(f"tu_{i}", "send_email", {"to": [f"x{i}@b.com"]}))
        for i in range(10)
    ]
    out = run(FakeClient(endless), max_turns=3)
    assert out["stopped"] == "max_turns"
    assert len(gate) == 3  # gated every time; just not forever


def test_a_hold_returns_as_information_not_error(gate, monkeypatch):
    monkeypatch.setattr(
        interpret_mod, "dispatch",
        lambda *a: {"held": True, "reason": "approval required by boundary", "log_id": 9},
    )
    client = FakeClient([
        resp(tool_use("tu_1", "send_email", {"to": ["a@b.com"]})),
        resp(text("That needs sign-off; I've queued it."), stop_reason="end_turn"),
    ])
    out = run(client)

    (result,) = client.requests[1]["messages"][-1]["content"]
    assert result["is_error"] is False  # a hold is a state, not a failure
    assert "do not retry" in result["content"]
    assert out["actions"][0]["held"] is True


def test_a_refusal_is_surfaced_not_retried(gate):
    client = FakeClient([resp(stop_reason="refusal")])
    out = run(client)
    assert out["stopped"] == "refusal" and gate == []
    assert len(client.requests) == 1


def test_every_proposal_in_a_run_shares_one_correlation_id(gate):
    client = FakeClient([
        resp(tool_use("tu_1", "send_email", {"to": ["a@b.com"]})),
        resp(tool_use("tu_2", "send_email", {"to": ["c@d.com"]})),
        resp(text("both sent"), stop_reason="end_turn"),
    ])
    out = run(client)
    ids = {r.correlation_id for r in gate}
    assert len(gate) == 2 and len(ids) == 1  # one thread, many actions
    assert out["correlation_id"] in ids  # and the caller gets the thread id


def test_assistant_content_is_echoed_back_whole(gate):
    # Thinking blocks must go back unchanged; appending the content list
    # verbatim is the contract. Verify identity, not a copy.
    first = resp(tool_use("tu_1", "send_email", {"to": ["a@b.com"]}))
    client = FakeClient([first, resp(text("done"), stop_reason="end_turn")])
    run(client)
    assert client.requests[1]["messages"][1]["content"] is first.content
