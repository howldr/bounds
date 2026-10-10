"""Live interpret: the full pipeline including a real model.

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile) and the Postgres
from live_smoke.py. The prompt deliberately asks for a two-recipient send
against a boundary capped at one — so the interesting output is a live deny
returned to the model, and whatever it does to adapt within bounds.
"""

import anthropic
import psycopg
from psycopg.rows import dict_row

# Importing registers the demo send_email action and brings the seed SQL.
from live_smoke import DSN, SEED, send_email

from bounds.catalogue import sync, verify
from bounds.interpret import interpret


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    sync(conn)
    verify(conn)
    conn.execute(SEED)

    out = interpret(
        conn,
        anthropic.Anthropic(),
        agent="invoice-followup",
        requested_by="operator",
        prompt=(
            "Invoice INV-042 (2,400 USD, due 2026-06-30) is overdue. Send a "
            "polite payment reminder to alice@example.com and bob@example.com."
        ),
        trigger_source="live-interpret",
        connectors={"send_email": send_email},
        resolve_secret=lambda ref: "demo-secret",
        data_touched=["invoices.read"],
    )

    print(f"stopped: {out['stopped']} after {out['turns']} turn(s)")
    print(f"\nmodel: {out['text']}\n")
    print("gated actions this run:")
    for a in out["actions"]:
        print("  ", {k: a[k] for k in ("allowed", "reason", "outcome", "log_id") if k in a})

    print("\naction_log (this trigger):")
    rows = conn.execute(
        """SELECT id, decision, deny_reason, outcome, action_params
           FROM action_log WHERE trigger_source = 'live-interpret' ORDER BY id"""
    ).fetchall()
    for r in rows:
        print("  ", r)


if __name__ == "__main__":
    main()
