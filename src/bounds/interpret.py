"""The interpret step: the only place a model belongs.

The model turns a natural-language trigger into concrete proposals. It is
offered exactly the tool list generated from the agent's boundary
(tools_for), and every tool_use it emits becomes an ActionRequest through
dispatch() — the same gate, the same log, the same scrutiny as a proposal
from a cron job. A deny returns to the model as an error tool result: it may
adapt within bounds, never argue its way past them.

The Anthropic client is injected, not imported. bounds takes no SDK
dependency; pass `anthropic.Anthropic()` (or anything with a compatible
`messages.create`). The manual loop is deliberate — every tool execution
here IS the gate, and that must be visible, not delegated to a runner hook.

Honesty note: this module calls a hosted model, so every request it emits
carries model_tier_used="cloud". A boundary with model_tier='local' will
deny all of them. That is the boundary working.
"""

import hashlib
import json
import uuid

from .executor import dispatch
from .models import ActionRequest
from .tools import tools_for

_SYSTEM = """You are {agent}, an operational agent acting on behalf of {requested_by}.

You act only through the tools provided. Every tool call is checked against
limits declared ahead of time by a system outside this conversation; a
refusal is final — adapt within the stated limits or report that you cannot
proceed. Never state that an action happened unless its tool result says so."""


def _idempotency_key(trigger_source: str, action: str, params: dict) -> str:
    """Derived from what would go out, not from which conversation proposed
    it: a retried trigger that lands on the same proposal collides in the
    log and replays, instead of sending twice."""
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        f"{trigger_source}|{action}|{payload}".encode()
    ).hexdigest()


def interpret(
    conn,
    client,
    *,
    agent: str,
    requested_by: str,
    prompt: str,
    trigger_source: str,
    connectors,
    resolve_secret,
    data_touched: list[str] | None = None,
    tenant: str = "default",
    model: str = "claude-opus-4-8",
    max_turns: int = 8,
) -> dict:
    """Run one natural-language trigger to completion.

    `data_touched` is declared by the caller, not the model — it describes
    what data was loaded into the prompt, and the caller is the one who
    loaded it. Self-reported scope would be no scope at all.

    Every proposal this run makes shares one correlation_id: the log can
    replay the whole thread of work, not just its individual actions.
    """
    tools = tools_for(conn, agent, tenant)
    correlation_id = uuid.uuid4().hex
    system = _SYSTEM.format(agent=agent, requested_by=requested_by)
    messages: list[dict] = [{"role": "user", "content": prompt}]
    actions: list[dict] = []

    for turn in range(1, max_turns + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system,
            tools=tools,
            messages=messages,
        )

        if resp.stop_reason == "refusal":
            return {"stopped": "refusal", "text": "", "actions": actions,
                    "turns": turn, "correlation_id": correlation_id}

        # Whole content back, thinking blocks included and unchanged —
        # that is the documented multi-turn contract.
        messages.append({"role": "assistant", "content": resp.content})

        proposals = [b for b in resp.content if b.type == "tool_use"]
        if not proposals:
            text = "".join(b.text for b in resp.content if b.type == "text")
            return {"stopped": "end_turn", "text": text, "actions": actions,
                    "turns": turn, "correlation_id": correlation_id}

        results = []
        for p in proposals:
            params = dict(p.input)
            req = ActionRequest(
                idempotency_key=_idempotency_key(trigger_source, p.name, params),
                trigger_type="nl",
                trigger_source=trigger_source,
                requested_by=requested_by,
                agent=agent,
                action=p.name,
                params=params,
                data_touched=list(data_touched or []),
                model_tier_used="cloud",
                tenant=tenant,
                correlation_id=correlation_id,
            )
            out = dispatch(conn, req, connectors, resolve_secret)
            actions.append(out)
            if out.get("held"):
                # Not an error: a would-be allow parked for a human. The
                # model must not retry — retrying mints nothing (same
                # idempotency key replays the hold row).
                content = json.dumps(
                    out | {"note": "held for human approval; do not retry"},
                    default=str,
                )
                results.append(
                    {"type": "tool_result", "tool_use_id": p.id, "content": content,
                     "is_error": False}
                )
                continue
            denied = not out.get("allowed", out.get("decision") == "allow")
            errored = out.get("outcome") == "error"
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": p.id,
                    "content": json.dumps(out, default=str),
                    "is_error": denied or errored,
                }
            )
        messages.append({"role": "user", "content": results})

    return {"stopped": "max_turns", "text": "", "actions": actions,
            "turns": max_turns, "correlation_id": correlation_id}
