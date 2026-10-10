"""Live smoke: the full pipeline against a real Postgres, no model involved.

Drives dispatch() directly with hand-built requests — allow, deny, replay —
and then reads the action log back. The connector is a stub that returns a
receipt; everything else (gate, log, triggers, idempotency) is real.

    docker run -d --name bounds-pg -e POSTGRES_PASSWORD=bounds \
        -e POSTGRES_DB=bounds -p 127.0.0.1:5433:5432 postgres:17-alpine
    psql < migrations/*.sql   (in order)
    python scripts/live_smoke.py
"""

import hashlib
import os
import secrets as pysecrets

import psycopg
from psycopg.rows import dict_row

from bounds import approvals, executor
from bounds.catalogue import Param, action, sync, verify
from bounds.executor import dispatch
from bounds.models import ActionRequest
from bounds.secrets import resolver_for

# 127.0.0.1, not localhost: Windows resolves localhost to ::1 first, and the
# container publishes on IPv4 only — libpq can hang on the IPv6 attempt
# instead of falling back. connect_timeout so a dead DB fails loud, not silent.
DSN = os.environ.get(
    "BOUNDS_DSN",
    "postgresql://postgres:bounds@127.0.0.1:5433/bounds?connect_timeout=10",
)

# Unique per run: the log is append-only and replays are by design, so a
# rerun must bring fresh keys to exercise fresh decisions. Request 4 reuses
# request 1's key on purpose — that one demonstrates the replay.
RUN = pysecrets.token_hex(4)


def reconcile_send_email(secret, params):
    # Demo reconciler: a real one would query the provider's sent-log by
    # message id. The stub "finds" the send, clearly labeled as demo.
    return ("success", {"demo": True, "note": "demo ledger says it went out"})


@action(
    "send_email",
    connector="demo",
    description="Send an email via the demo connector.",
    params={
        "to": Param("list[str]", feeds="max_recipients", desc="Recipient addresses."),
        "subject": Param("str", required=False),
        "body": Param("str", required=False),
    },
    reconcile=reconcile_send_email,
)
def send_email(secret, to, subject=None, body=None):
    # Stub connector: proves the dispatch path without sending anything.
    # The secret's fingerprint (never the secret) goes in the receipt —
    # proof that the real bytes arrived here from the keyring.
    fp = hashlib.sha256(secret.encode()).hexdigest()[:8]
    return {"demo": True, "delivered_to": to, "subject": subject, "secret_sha256_8": fp}


SEED = """
INSERT INTO credential (ref, connector, scope, secret_uri)
VALUES ('demo-send-only', 'demo', '{send_email}', 'keyring://bounds/demo-send-only')
ON CONFLICT (ref) DO UPDATE SET secret_uri = EXCLUDED.secret_uri;

INSERT INTO boundary (agent, version, data_scope, actions_allowed, limits,
                      model_tier, credential_ref, created_by)
VALUES ('invoice-followup', 1, '{invoices.read}', '{send_email}',
        '{"max_recipients": 1, "max_per_day": 5}', 'cloud',
        'demo-send-only', 'operator')
ON CONFLICT (tenant, agent, version) DO NOTHING;

-- A boundary that trusts nothing to run unattended: every action under it
-- is held for a named human before it executes.
INSERT INTO boundary (agent, version, data_scope, actions_allowed, limits,
                      model_tier, credential_ref, created_by, approval_required)
VALUES ('refund-bot', 1, '{invoices.read}', '{send_email}',
        '{"max_recipients": 1}', 'cloud', 'demo-send-only', 'operator', true)
ON CONFLICT (tenant, agent, version) DO NOTHING;
"""


def req(key, params, agent="invoice-followup", tenant="default"):
    return ActionRequest(
        idempotency_key=key, trigger_type="event", trigger_source="live-smoke",
        requested_by="operator", agent=agent, action="send_email",
        params=params, data_touched=["invoices.read"],
        tenant=tenant, correlation_id=f"smoke-run-{RUN}",
    )


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    connectors = {"send_email": send_email}
    # The real chain: ref -> credential row -> secret_uri -> OS keyring.
    resolve = resolver_for(conn)

    sync(conn)
    verify(conn)
    print("catalogue synced + verified")

    conn.execute(SEED)
    print(f"boundary + credential seeded (run {RUN})")

    k = lambda n: f"smoke-{RUN}-{n}"  # noqa: E731

    print("\n1. inside every limit ->")
    print("  ", dispatch(conn, req(k(1), {"to": ["a@example.com"], "subject": "hi"}),
                          connectors, resolve))

    print("2. two recipients against a cap of 1 ->")
    print("  ", dispatch(conn, req(k(2), {"to": ["a@example.com", "b@example.com"]}),
                          connectors, resolve))

    print("3. params the schema does not declare ->")
    print("  ", dispatch(conn, req(k(3), {"recipients": ["a@example.com"]}),
                          connectors, resolve))

    print("4. same idempotency key as 1 ->")
    print("  ", dispatch(conn, req(k(1), {"to": ["a@example.com"], "subject": "hi"}),
                          connectors, resolve))

    print("\n5. refund-bot's boundary demands approval ->")
    held = dispatch(conn, req(k(5), {"to": ["c@example.com"], "subject": "refund"},
                              agent="refund-bot"), connectors, resolve)
    print("  ", held)

    print("6. a named human approves; release re-checks and executes ->")
    approvals.approve(conn, held["log_id"], "operator", "verified against invoice")
    print("  ", approvals.release(conn, held["log_id"], connectors, resolve))

    print("7. a second hold, rejected ->")
    held2 = dispatch(conn, req(k(7), {"to": ["d@example.com"], "subject": "refund"},
                               agent="refund-bot"), connectors, resolve)
    approvals.reject(conn, held2["log_id"], "operator", "no matching invoice")
    print("  ", approvals.release(conn, held2["log_id"], connectors, resolve))

    print("8. a crash between commit and execute leaves outcome NULL; reconcile closes it ->")
    crashed = conn.execute(
        """INSERT INTO action_log
               (idempotency_key, trigger_type, trigger_source, requested_by, agent,
                boundary_version, action_requested, action_params, decision, data_touched)
           VALUES (%s, 'event', 'live-smoke', 'operator', 'invoice-followup',
                   1, 'send_email', '{"to": ["crash@example.com"]}', 'allow', '{}')
           RETURNING id""",
        (k(8),),
    ).fetchone()["id"]
    print("   simulated crash row:", crashed, "(allow, outcome NULL)")
    print("  ", executor.reconcile(conn, resolve, older_than_seconds=0))

    print("9. same agent name, different tenant -> nothing is granted ->")
    print("  ", dispatch(conn, req(k(9), {"to": ["a@example.com"]}, tenant="acme"),
                          connectors, resolve))

    print("\naction_log (this run):")
    rows = conn.execute(
        """SELECT id, idempotency_key, decision, deny_reason, outcome
           FROM action_log WHERE idempotency_key LIKE %s ORDER BY id""",
        (f"smoke-{RUN}-%",),
    ).fetchall()
    for r in rows:
        print("  ", r)


if __name__ == "__main__":
    main()
