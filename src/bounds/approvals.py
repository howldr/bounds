"""Resolving holds: a human records a verdict, then the hold is released.

Two steps on purpose. approve()/reject() write the verdict — that is the
human act, and it is append-only, one per held row. release() finalises:
it re-runs the boundary check as of *now* (an approval satisfies the
approval gate and nothing else — if max_per_day filled up while the hold
sat, the release denies), flips the decision under the 004 trigger's
guard, and only then executes.

There is no override path here. An approval can only attach to a hold —
a would-be allow — never to a deny. The trigger enforces that in
Postgres, not in this module.
"""

from psycopg.types.json import Json

from .check import check
from .models import ActionRequest


def approve(conn, log_id: int, by: str, reason: str | None = None) -> None:
    _verdict(conn, log_id, "approved", by, reason)


def reject(conn, log_id: int, by: str, reason: str | None = None) -> None:
    _verdict(conn, log_id, "rejected", by, reason)


def _verdict(conn, log_id: int, verdict: str, by: str, reason: str | None) -> None:
    conn.execute(
        """INSERT INTO approval (action_log_id, verdict, approved_by, reason)
           VALUES (%s, %s, %s, %s)""",
        (log_id, verdict, by, reason),
    )


def release(conn, log_id: int, connectors, resolve_secret) -> dict:
    """Finalise a held row that has a verdict. Mirrors dispatch(): decide
    and commit inside the transaction, execute outside it."""
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM action_log WHERE id = %s FOR UPDATE", (log_id,)
        ).fetchone()
        if row is None:
            return {"error": f"no log row {log_id}"}
        if row["decision"] != "hold":
            return {"error": f"log {log_id} is {row['decision']}, not held"}

        a = conn.execute(
            "SELECT * FROM approval WHERE action_log_id = %s", (log_id,)
        ).fetchone()
        if a is None:
            return {"held": True, "log_id": log_id, "reason": "no verdict recorded yet"}

        if a["verdict"] == "rejected":
            reason = f"rejected by {a['approved_by']}" + (
                f": {a['reason']}" if a["reason"] else ""
            )
            conn.execute(
                "UPDATE action_log SET decision = 'deny', deny_reason = %s WHERE id = %s",
                (reason, log_id),
            )
            return {"allowed": False, "reason": reason, "log_id": log_id}

        # Approved. Re-check as of now — the hold satisfied every gate when
        # it was parked, but the world moved while it waited.
        req = ActionRequest(
            idempotency_key=row["idempotency_key"],
            trigger_type=row["trigger_type"],
            trigger_source=row["trigger_source"],
            requested_by=row["requested_by"],
            agent=row["agent"],
            action=row["action_requested"],
            params=row["action_params"],
            data_touched=row["data_touched"],
            model_tier_used=row["model_tier_used"],
            tenant=row["tenant"],
            correlation_id=row["correlation_id"],
        )
        d = check(conn, req, approved=True)
        if not d.allow:
            reason = f"approved by {a['approved_by']} but re-check denied: {d.reason}"
            conn.execute(
                "UPDATE action_log SET decision = 'deny', deny_reason = %s WHERE id = %s",
                (reason, log_id),
            )
            return {"allowed": False, "reason": reason, "log_id": log_id}

        conn.execute(
            "UPDATE action_log SET decision = 'allow', deny_reason = NULL WHERE id = %s",
            (log_id,),
        )

    # Outside the transaction: never hold a lock across a network call.
    try:
        secret = resolve_secret(d.credential_ref)
        result = connectors[req.action](secret, **req.params)
        outcome, payload = "success", result
    except Exception as e:  # noqa: BLE001 — the log is the place this goes
        outcome, payload = "error", {"error": repr(e)}

    conn.execute(
        "UPDATE action_log SET outcome = %s, result = %s WHERE id = %s",
        (outcome, Json(payload), log_id),
    )
    return {
        "allowed": True,
        "approved_by": a["approved_by"],
        "outcome": outcome,
        "result": payload,
        "log_id": log_id,
    }
