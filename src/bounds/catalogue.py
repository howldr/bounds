"""The action catalogue: the list a human wrote of things an agent can do.

Nothing here decides whether an action is *allowed* — that's check.py. This
module decides what an action *is*: its name, its connector, the shape of its
params, and which limits each param feeds.

That last part is the load-bearing one. A limit like `max_recipients` is
meaningless unless something tells it which param to count. Binding that by
convention ("it reads params['to']") means a request whose params are shaped
differently reads as zero, and zero is inside every limit. So the binding is
declared, and a limit with nothing feeding it is a config error, not a pass.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Limits that count something in the request's params.
PARAM_LIMITS = frozenset({"max_recipients", "max_spend_usd"})
# Limits that are properties of the request itself, not of any param.
AMBIENT_LIMITS = frozenset({"max_per_day", "hours_allowed"})
# An unrecognised limits key must not silently disable a limit. A typo like
# `max_recipient` should fail when the boundary is authored, not at 3am.
KNOWN_LIMITS = PARAM_LIMITS | AMBIENT_LIMITS


class BoundaryConfigError(Exception):
    """Raised on write, not on check. The boundary or the action is malformed."""


# --- param types --------------------------------------------------------
# Deliberately tiny. Full JSON Schema would mean a dependency, and the only
# question being asked here is "did the model hand us the shape we declared".

_TYPES: dict[str, Callable[[Any], bool]] = {
    "str": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
    # bool is an int in Python; a flag is not a quantity.
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "list[str]": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
}

# Types a limit can be measured against. A str has no magnitude to cap.
MEASURABLE = frozenset({"number", "list[str]"})


def measure(type_: str, value: Any) -> float:
    """What this param contributes to the limit it feeds."""
    if type_ == "list[str]":
        return float(len(value))
    if type_ == "number":
        return float(value)
    raise BoundaryConfigError(f"type {type_!r} cannot feed a limit")


@dataclass(frozen=True)
class Param:
    type: str
    required: bool = True
    feeds: str | None = None  # a PARAM_LIMITS key
    desc: str | None = None  # surfaced to the model in the generated tool list


@dataclass(frozen=True)
class Action:
    name: str
    connector: str
    description: str  # what the model triggers on; empty = unusable, rejected
    params: dict[str, Param]
    fn: Callable
    # Asks the downstream "did this actually happen?" for a row whose
    # outcome is unknown (crash between commit and execute). Returns
    # (outcome, result) or None for "cannot say". Optional: an action
    # without one leaves its unknowns unknown, which is honest too.
    reconcile: Callable | None = None

    def schema(self) -> dict:
        """The jsonb form. This is what `check` reads and what `sync` writes."""
        return {
            n: {"type": p.type, "required": p.required, "feeds": p.feeds, "desc": p.desc}
            for n, p in self.params.items()
        }


REGISTRY: dict[str, Action] = {}


def action(
    name: str,
    *,
    connector: str,
    description: str,
    params: dict[str, Param],
    reconcile: Callable | None = None,
):
    """Register a callable as an action. Everything is checked here, at import
    time, so a malformed action can't reach a boundary."""

    def deco(fn: Callable) -> Callable:
        if name in REGISTRY:
            raise BoundaryConfigError(f"duplicate action {name!r}")
        if not description.strip():
            # The description is how a model decides when to use a tool.
            # An action without one is a capability nobody can reach for.
            raise BoundaryConfigError(f"{name}: description is required")
        if reconcile is not None and not callable(reconcile):
            raise BoundaryConfigError(f"{name}: reconcile must be callable")

        for pname, spec in params.items():
            if spec.type not in _TYPES:
                raise BoundaryConfigError(
                    f"{name}.{pname}: unknown type {spec.type!r}"
                )
            if spec.feeds is not None:
                if spec.feeds not in PARAM_LIMITS:
                    raise BoundaryConfigError(
                        f"{name}.{pname}: {spec.feeds!r} is not a param limit"
                    )
                if spec.type not in MEASURABLE:
                    raise BoundaryConfigError(
                        f"{name}.{pname}: type {spec.type!r} cannot feed "
                        f"{spec.feeds!r}"
                    )

        # The declared params and the callable must agree exactly, or the
        # schema is describing something other than what runs.
        got = set(inspect.signature(fn).parameters)
        want = {"secret", *params}
        if got != want:
            raise BoundaryConfigError(
                f"{name}: signature {sorted(got)} != declared {sorted(want)}"
            )

        REGISTRY[name] = Action(name, connector, description, dict(params), fn, reconcile)
        return fn

    return deco


# --- request-side validation -------------------------------------------


def validate_params(schema: dict, params: dict) -> str | None:
    """None if params match the schema, else why not.

    Unknown params are rejected rather than ignored: an unrecognised key means
    the model proposed something we don't model, and the limits are only
    reading the keys we do.
    """
    if unknown := sorted(set(params) - set(schema)):
        return f"unknown params {unknown}"
    for name, spec in schema.items():
        if name not in params:
            if spec.get("required", True):
                return f"missing required param {name!r}"
            continue
        if not _TYPES[spec["type"]](params[name]):
            return f"param {name!r} expected {spec['type']}"
    return None


def feeders(schema: dict, limit: str) -> list[tuple[str, str]]:
    """(param name, type) for every param feeding `limit`. Sorted, because a
    deny reason that varies between runs isn't a deterministic check."""
    return sorted(
        (n, s["type"]) for n, s in schema.items() if s.get("feeds") == limit
    )


# --- authoring-side validation -----------------------------------------


def validate_limits(limits: dict) -> None:
    if unknown := sorted(set(limits) - KNOWN_LIMITS):
        raise BoundaryConfigError(f"unknown limit keys: {unknown}")


def validate_boundary(limits: dict, actions_allowed: list[str]) -> None:
    """The authoring-time counterpart of `check`. Same questions, asked while
    someone is still looking at the screen."""
    validate_limits(limits)
    for a in actions_allowed:
        if a not in REGISTRY:
            raise BoundaryConfigError(f"unknown action {a!r}")
    for limit in sorted(PARAM_LIMITS & set(limits)):
        for a in actions_allowed:
            if not feeders(REGISTRY[a].schema(), limit):
                raise BoundaryConfigError(
                    f"limit {limit!r} has no param feeding it on action {a!r}"
                )


# --- the DB copy --------------------------------------------------------
# The registry is the source of truth: the schema is derived from the code that
# runs, so the two can't drift. The `action` table exists so a boundary written
# by hand in psql can be checked against it. `verify` is what keeps the copy
# honest, and should run at startup.


def sync(conn) -> None:
    from psycopg.types.json import Json

    for a in REGISTRY.values():
        conn.execute(
            """INSERT INTO action (name, connector, description, params_schema)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (name) DO UPDATE
                 SET connector = EXCLUDED.connector,
                     description = EXCLUDED.description,
                     params_schema = EXCLUDED.params_schema""",
            (a.name, a.connector, a.description, Json(a.schema())),
        )


def verify(conn) -> None:
    """Refuse to boot on drift. A catalogued action with no implementation is a
    boundary granting something that can't run; an implementation missing from
    the catalogue is a capability no boundary can name."""
    rows = conn.execute(
        "SELECT name, connector, description, params_schema FROM action"
    ).fetchall()
    db = {r["name"]: (r["connector"], r["description"], r["params_schema"]) for r in rows}
    reg = {n: (a.connector, a.description, a.schema()) for n, a in REGISTRY.items()}

    problems = [
        *(f"{n}: in catalogue, no implementation registered" for n in sorted(set(db) - set(reg))),
        *(f"{n}: implemented, not in catalogue — run sync" for n in sorted(set(reg) - set(db))),
        *(
            f"{n}: catalogue and implementation disagree"
            for n in sorted(set(db) & set(reg))
            if db[n] != reg[n]
        ),
    ]
    if problems:
        raise BoundaryConfigError("action catalogue drift:\n  " + "\n  ".join(problems))
