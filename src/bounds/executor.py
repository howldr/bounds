"""Log the decision, commit, then execute.

Ordering is the whole point. If the process dies mid-execute you get a row
saying `allow` with `outcome IS NULL` — an honest "I don't know if this
happened", which is recoverable. The reverse ordering gives you a sent email
with no record, which isn't.
"""

from psycopg.types.json import Json

from .catalogue import REGISTRY
from .check import check
from .models import ActionRequest

_INSERT = """
INSERT INTO action_log
    (idempotency_key, trigger_type, trigger_source, requested_by, agent,
     boundary_version, action_requested, action_params, decision,
     deny_reason, data_touched, model_tier_used, tenant, correlation_id)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
RETURNING id
"""


def dispatch(conn, req: ActionRequest, connectors, resolve_secret) -> dict:
    """`connectors` maps action name -> callable(secret, **params).
    `resolve_secret` maps a credential ref -> the actual secret, and is the
    only place a secret enters the process."""

    with conn.transaction():
        prior = conn.execute(
            "SELECT * FROM action_log WHERE idempotency_key = %s",
            (req.idempotency_key,),
        ).fetchone()
        if prior:
            return {"replayed": True, **dict(prior)}

        d = check(conn, req)

        decision = "allow" if d.allow else ("hold" if d.hold else "deny")
        row_id = conn.execute(
            _INSERT,
            (
                req.idempotency_key, req.trigger_type, req.trigger_source,
                req.requested_by, req.agent, d.boundary_version, req.action,
                Json(req.params), decision,
                d.reason, req.data_touched, req.model_tier_used,
                req.tenant, req.correlation_id,
            ),
        ).fetchone()["id"]

        if d.hold:
            # A would-be allow, parked. approvals.release() picks it up
            # after a human records a verdict.
            return {"held": True, "reason": d.reason, "log_id": row_id}
        if not d.allow:
            return {"allowed": False, "reason": d.reason, "log_id": row_id}

    # Outside the transaction: never hold a lock across a network call.
    try:
        secret = resolve_secret(d.credential_ref)
        result = connectors[req.action](secret, **req.params)
        outcome, payload = "success", result
    except Exception as e:  # noqa: BLE001 — the log is the place this goes
        outcome, payload = "error", {"error": repr(e)}

    conn.execute(
        "UPDATE action_log SET outcome = %s, result = %s WHERE id = %s",
        (outcome, Json(payload), row_id),
    )
    return {
        "allowed": True,
        "outcome": outcome,
        "result": payload,
        "log_id": row_id,
    }


def reconcile(conn, resolve_secret, older_than_seconds: int = 300) -> list[dict]:
    """Close the honest "I don't know" rows.

    An allow with outcome NULL means the process died between committing the
    decision and finishing the execute. The row is recoverable by design:
    ask the downstream whether the side effect happened, via the action's
    registered reconcile callable. No reconciler, or a reconciler that
    can't say, leaves the row unknown — an unknown that stays unknown is
    still more honest than a guess.

    The grace period exists because a NULL outcome is also just an execute
    in flight; don't reconcile what hasn't had time to finish.
    """
    rows = conn.execute(
        """SELECT * FROM action_log
           WHERE decision = 'allow' AND outcome IS NULL
             AND ts < now() - make_interval(secs => %s)""",
        (older_than_seconds,),
    ).fetchall()

    report = []
    for row in rows:
        entry = {"log_id": row["id"], "action": row["action_requested"]}
        act = REGISTRY.get(row["action_requested"])
        if act is None or act.reconcile is None:
            report.append(entry | {"state": "unresolved", "why": "no reconciler"})
            continue

        # The boundary row that allowed this is versioned and never edited,
        # so (agent, boundary_version) still names the credential it used.
        b = conn.execute(
            "SELECT credential_ref FROM boundary WHERE agent = %s AND version = %s",
            (row["agent"], row["boundary_version"]),
        ).fetchone()
        try:
            secret = resolve_secret(b["credential_ref"])
            found = act.reconcile(secret, row["action_params"])
        except Exception as e:  # noqa: BLE001 — an unresolved row, not a crash
            report.append(entry | {"state": "unresolved", "why": repr(e)})
            continue

        if found is None:
            report.append(entry | {"state": "unresolved", "why": "connector cannot say"})
            continue

        outcome, result = found
        conn.execute(
            "UPDATE action_log SET outcome = %s, result = %s WHERE id = %s",
            (outcome, Json(dict(result) | {"reconciled": True}), row["id"]),
        )
        report.append(entry | {"state": outcome})
    return report
