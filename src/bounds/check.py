"""The boundary check.

Deterministic. No model participates. Default deny. The only impurity is the
rate-limit query, which reads the action log — deliberately, so there is one
source of truth for "how many have gone out today".

Connection contract: rows are mappings. Connect with
psycopg.connect(dsn, row_factory=psycopg.rows.dict_row).
"""

from datetime import UTC, datetime

from .catalogue import (
    PARAM_LIMITS,
    BoundaryConfigError,  # noqa: F401 — re-exported; this is where callers look
    KNOWN_LIMITS,
    feeders,
    measure,
    validate_boundary,  # noqa: F401 — re-exported
    validate_limits,  # noqa: F401 — re-exported
    validate_params,
)
from .models import ActionRequest, Decision


def check(
    conn, req: ActionRequest, now: datetime | None = None, *, approved: bool = False
) -> Decision:
    """`approved=True` means a named human's verdict for exactly this request
    is already on record — the release path sets it after verifying the
    approval row. It satisfies the approval gate and nothing else: every
    other limit is re-checked as of now, not as of the hold."""
    # Aware, or the comparison with a timestamptz expiry raises instead of
    # denying. Consequence: hours_allowed is interpreted in UTC.
    now = now or datetime.now(UTC)

    b = conn.execute(
        "SELECT * FROM boundary WHERE tenant = %s AND agent = %s AND active",
        (req.tenant, req.agent),
    ).fetchone()
    if b is None:
        return Decision(False, "no active boundary for agent")

    v = b["version"]

    # Belt and braces: a boundary that slipped past validate_limits fails closed.
    unknown = set(b["limits"]) - KNOWN_LIMITS
    if unknown:
        return Decision(False, f"boundary has unknown limit keys: {sorted(unknown)}", v)

    # --- action scope ---------------------------------------------------
    if req.action not in b["actions_allowed"]:
        return Decision(False, f"action {req.action!r} not in actions_allowed", v)

    # --- the action must be one we catalogued ---------------------------
    # The 002 trigger should make this unreachable. It is here because the
    # alternative to an unreachable deny is an unhandled None.
    act = conn.execute(
        "SELECT name, connector, params_schema FROM action WHERE name = %s",
        (req.action,),
    ).fetchone()
    if act is None:
        return Decision(False, f"action {req.action!r} is not in the catalogue", v)
    schema = act["params_schema"]

    # --- params shape ---------------------------------------------------
    # Before the limits, not after: every param limit below counts a field this
    # has just proved exists and is the declared type. A params dict we don't
    # recognise is a request we can't reason about, and an unreasoned request
    # is a deny — never a pass by way of counting zero of something.
    if (bad := validate_params(schema, req.params)) is not None:
        return Decision(False, f"params for {req.action!r}: {bad}", v)

    # --- data scope -----------------------------------------------------
    out_of_scope = set(req.data_touched) - set(b["data_scope"])
    if out_of_scope:
        return Decision(False, f"data scope violation: {sorted(out_of_scope)}", v)

    # --- model scope ----------------------------------------------------
    if b["model_tier"] == "local" and req.model_tier_used != "local":
        return Decision(False, "boundary requires local model tier", v)

    # --- limits that count a param --------------------------------------
    lim = b["limits"]

    for name in sorted(PARAM_LIMITS & set(lim)):  # sorted: deny reasons are stable
        fed = feeders(schema, name)
        if not fed:
            # The boundary is asking for a guarantee this action cannot give.
            # Denying is the only honest answer.
            return Decision(
                False,
                f"boundary sets {name} but action {req.action!r} has no param "
                f"feeding it",
                v,
            )
        # Summed, so `to` + `cc` + `bcc` all count against max_recipients
        # rather than each getting its own allowance.
        total = sum(
            measure(t, req.params[p]) for p, t in fed if p in req.params
        )
        if total > lim[name]:
            return Decision(False, f"{name} {total:g} > {lim[name]}", v)

    # --- ambient limits -------------------------------------------------
    if (mpd := lim.get("max_per_day")) is not None:
        used = conn.execute(
            """SELECT count(*) AS n FROM action_log
               WHERE tenant = %s AND agent = %s AND action_requested = %s
                 AND decision = 'allow' AND ts > now() - interval '1 day'""",
            (req.tenant, req.agent, req.action),
        ).fetchone()["n"]
        if used >= mpd:
            return Decision(False, f"max_per_day reached ({used}/{mpd})", v)

    if (hrs := lim.get("hours_allowed")) is not None:  # [9, 17]
        if not (hrs[0] <= now.hour < hrs[1]):
            return Decision(False, f"outside hours_allowed {hrs}", v)

    # --- the credential must not exceed the boundary ---------------------
    # Catches the case where a credential's scope gets widened later and the
    # boundary was the only thing holding it back. Four lines, fails closed.
    cred = conn.execute(
        "SELECT * FROM credential WHERE ref = %s", (b["credential_ref"],)
    ).fetchone()
    if cred is None:
        return Decision(False, "credential_ref does not resolve", v)
    if cred["expires_at"] is not None and cred["expires_at"] <= now:
        return Decision(False, "credential expired", v)
    if not set(cred["scope"]).issubset(set(b["actions_allowed"])):
        return Decision(False, "credential scope exceeds boundary — refusing", v)

    # --- approval: the last gate ----------------------------------------
    # After everything else, so a hold is always a would-be allow. No
    # credential is surfaced on a hold — nothing to hold it *with* until
    # a human says so.
    if b["approval_required"] and not approved:
        return Decision(False, "approval required by boundary", v, hold=True)

    return Decision(True, None, v, b["credential_ref"])
