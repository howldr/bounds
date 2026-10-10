"""The tool list, generated from the boundary.

"Free to reason, limited in action": the menu the model is offered and the
menu the gate enforces are the same database rows. An action outside the
boundary is never offered, so the model can't select it — only hallucinate a
tool it was never given, and that proposal lands on the same deny in check().

Nothing here grants anything. Offering is a convenience for the interpret
step; check() remains the only gate, and it neither knows nor cares what the
model was shown.

Output is a list of plain dicts in the Messages API tool shape —
{name, description, input_schema} — deliberately without an SDK dependency.
It is deterministic (sorted names, sorted keys): a tool list that varies
between runs invalidates any prompt cache sitting in front of it.
"""

from .catalogue import PARAM_LIMITS, BoundaryConfigError, feeders

# catalogue types -> JSON Schema. One entry per _TYPES key, by construction.
_JSON_TYPES = {
    "str": {"type": "string"},
    "bool": {"type": "boolean"},
    "number": {"type": "number"},
    "list[str]": {"type": "array", "items": {"type": "string"}},
}


def _input_schema(params_schema: dict) -> dict:
    props = {}
    for name in sorted(params_schema):
        spec = params_schema[name]
        prop = dict(_JSON_TYPES[spec["type"]])
        if spec.get("desc"):
            prop["description"] = spec["desc"]
        props[name] = prop
    return {
        "type": "object",
        "properties": props,
        "required": sorted(
            n for n, s in params_schema.items() if s.get("required", True)
        ),
        # The gate denies unknown params; saying so here means the model
        # finds out at proposal time, not from a deny receipt.
        "additionalProperties": False,
    }


def _limits_note(limits: dict, params_schema: dict) -> str:
    """The caps, stated to the model. Advisory here, enforced in check() —
    exceeding one is a deny, but a model that knows the cap can plan a
    proposal that fits instead of burning a request finding out."""
    parts = []
    for name in sorted(limits):
        value = limits[name]
        if name in PARAM_LIMITS:
            fed = ", ".join(p for p, _ in feeders(params_schema, name))
            parts.append(f"{name}={value} (summed across: {fed})")
        elif name == "hours_allowed":
            parts.append(f"hours_allowed={value[0]}:00–{value[1]}:00")
        else:
            parts.append(f"{name}={value}")
    if not parts:
        return ""
    return (
        " Limits enforced on every call (exceeding any is a refusal): "
        + "; ".join(parts)
        + "."
    )


def tools_for(conn, agent: str, tenant: str = "default") -> list[dict]:
    """The tools this agent may be offered, from its active boundary.

    No boundary means an empty list — the offering side of default deny. An
    agent with nothing granted isn't handed a menu of things to be refused.
    """
    b = conn.execute(
        "SELECT * FROM boundary WHERE tenant = %s AND agent = %s AND active",
        (tenant, agent),
    ).fetchone()
    if b is None:
        return []

    tools = []
    for name in sorted(b["actions_allowed"]):
        act = conn.execute(
            "SELECT name, description, params_schema FROM action WHERE name = %s",
            (name,),
        ).fetchone()
        if act is None:
            # The 002 trigger makes this unreachable for boundaries written
            # through Postgres. If it happens anyway, the boundary is
            # malformed — and unlike check(), this isn't the gate, so the
            # honest response is loud, not a quiet deny.
            raise BoundaryConfigError(
                f"boundary for {agent!r} grants uncatalogued action {name!r}"
            )
        schema = act["params_schema"]
        tools.append(
            {
                "name": act["name"],
                "description": act["description"]
                + _limits_note(b["limits"], schema),
                "input_schema": _input_schema(schema),
            }
        )
    return tools
