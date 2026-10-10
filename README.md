# bounds

*The gate half of **Leaps and Bounds**, by howldr — move by leaps, stay in
bounds. `leaps` is the workflow registry; this repo is the boundary.*

A deterministic control plane for agent actions. An agent proposes an action; a
plain boolean function checks it against limits declared **ahead of time**; the
action executes with a credential scoped to those limits; everything is logged.

```text
Trigger → Interpret → Boundary Check → Execute → Log
```

## Design commitments

These are the load-bearing ones. Changing any of them is a redesign, not a refactor.

1. **Default deny.** No boundary, no action. Deny is terminal — there is no
   override path. An approval can release a *held* would-be allow (see
   Approvals); it can never turn a deny into an allow. Widening a boundary is
   a deliberate, versioned change.
2. **The check is deterministic.** No model participates in the decision to
   allow. The model decides *what to propose*, never *what is permitted*.
3. **Agents don't hold credentials, workers do.** The limit is physical: an
   `send_email`-only token, not a mailbox credential the model is asked nicely
   not to misuse.
4. **The log is the rate limiter.** `max_per_day` is a `COUNT(*)` over
   `action_log` in the same transaction as the insert. One source of truth.
5. **Decide, commit, then execute.** A crash mid-execute leaves an honest
   "I don't know if this happened" row, which is recoverable. The reverse
   ordering leaves a sent email with no record, which isn't.
6. **An action is only what someone catalogued.** The model reasons freely and
   proposes whatever it likes; the set of things it can *do* is a list a human
   wrote. Nothing is reachable by being named convincingly.

## Actions

An action is a registered callable plus a declared param shape:

```python
@action("send_email", connector="gmail",
        description="Send an email via the workspace Gmail account.",
        params={
            "to":      Param("list[str]", feeds="max_recipients",
                             desc="Recipient email addresses."),
            "subject": Param("str", required=False),
        })
def send_email(secret, to, subject=None): ...
```

`feeds` is the load-bearing part. A limit like `max_recipients` has to count
*something*, and binding that by convention — "it reads `params['to']`" — means
a request shaped differently reads as zero, and zero is inside every limit. The
gate would return allow on a request it never understood. So the binding is
declared:

- Params are validated against the schema **before** any limit runs. Unknown,
  missing, or wrong-typed params are a deny.
- Every param feeding a limit is summed, so `to` + `cc` + `bcc` share one
  recipient allowance rather than each getting their own.
- A boundary setting a limit no param feeds is a **deny**, not a silent pass.
  `max_spend_usd` on an action with no money param is an author believing they
  capped something they didn't.

The registry is the source of truth; the `action` table is a synced copy so a
boundary written by hand in psql can be checked against it. `catalogue.verify()`
refuses to boot on drift between the two.

## The tool list

`tools.tools_for(conn, agent)` generates the model's tool menu from the
agent's active boundary: one Messages-API-shaped tool dict
(`name`, `description`, `input_schema`) per granted action, with the boundary's
limits stated in the description so the model can plan a proposal that fits
instead of burning a request finding out.

This is how "free to reason, limited in action" is enforced at the offering
layer: the menu the model sees and the menu the gate checks are the same
database rows. An agent with no boundary gets an empty list. Nothing about the
offer *grants* anything — `check()` remains the only gate, and a hallucinated
tool that was never offered lands on the same deny as everything else.

Plain dicts, no SDK dependency.

## Interpret

`interpret.interpret(conn, client, ...)` runs one natural-language trigger to
completion: it offers the model the boundary-generated tool list, and every
`tool_use` the model emits becomes an `ActionRequest` through `dispatch()` —
the same gate, log, and scrutiny as a proposal from a cron job. A deny goes
back to the model as an error tool result; it can adapt within bounds, never
argue past them.

Structural choices, all deliberate:

- **The Anthropic client is injected, not imported.** The core package keeps
  zero SDK dependency (`pip install bounds[interpret]` adds one); the model
  stays peripheral by construction, not convention.
- **`model_tier_used="cloud"`, always.** This module calls a hosted model and
  says so — a `model_tier='local'` boundary denies every proposal from it.
  That is the boundary working.
- **`data_touched` is declared by the caller**, describing what data was
  loaded into the prompt. Self-reported scope would be no scope at all.
- **Idempotency keys derive from the outgoing proposal**
  (`trigger_source` + action + sorted params), not the conversation — a
  retried trigger that lands on the same proposal collides and replays
  instead of sending twice.
- **`max_turns` bounds the loop.** Every turn is gated either way; this just
  refuses to loop forever.

## Secrets

bounds never stores a secret. `credential.secret_uri` is a **pointer** into a
backend whose job is holding secrets; `secrets.resolve()` dereferences it at
the moment of use and nowhere else:

```text
action_log.credential_ref  ->  'demo-send-only'      (all the log ever holds)
credential.secret_uri      ->  'keyring://bounds/…'  (an address, still not a secret)
the backend                ->  the actual bytes      (recovered at use, then gone)
```

Schemes: `env:NAME` (dev/smoke only) and `keyring://service/entry` (OS
keyring — Windows Credential Manager, macOS Keychain, libsecret; needs
`pip install bounds[secrets]`). `vault://` / cloud secret managers are future
schemes behind the same interface.

`secrets.resolver_for(conn)` is the production `resolve_secret` for
`dispatch()`: ref → credential row → uri → backend. A pointer that doesn't
dereference **raises** — never returns empty. Note when that lands: dispatch
resolves *after* the allow is committed, so a broken pointer logs
`allow + outcome=error`. The decision was right, the execution failed, and
the log says exactly that.

## Reconciliation

An allow with `outcome IS NULL` is the honest "I don't know if this
happened" row that decide-commit-execute leaves behind on a crash.
`executor.reconcile(conn, resolve_secret)` closes them: each action may
register a `reconcile=` callable that asks the downstream whether the side
effect actually occurred (`(secret, params) -> (outcome, result) | None`).
No reconciler, a reconciler that can't say, or a reconciler that raises all
leave the row unknown — an unknown that stays unknown beats a guess. A
grace period keeps in-flight executes out of scope.

## Approvals

A boundary with `approval_required` holds everything under it for a named
human. **hold is a would-be allow**: requests that would deny still deny —
only proposals that cleared every other gate are worth a human's attention.
A held row surfaces no credential; there is nothing to hold it *with* until
someone says so.

`approvals.approve()/reject()` record the verdict (append-only, one per
held row); `approvals.release()` finalises — it **re-checks as of now**
(an approval satisfies the approval gate and nothing else; if `max_per_day`
filled while the hold sat, the release denies), then flips the decision and
executes. Postgres guards the state machine: an approval can only attach to
a hold, and a decision only moves hold → allow/deny with a verdict standing
behind it. allow/deny are final. There is no override path — approving a
deny is a trigger error, not an API call.

## Tenants and correlation

`tenant` is the isolation boundary above agents; every table that names an
agent or a credential names an owner. It defaults to `'default'`, which stays
the tenant for single-tenant deployments — nobody has to know the column
exists until they have two customers.

The column only means something because uniqueness moved inside it: one
active boundary per `(tenant, agent)`, and the `max_per_day` count is per
tenant. Both matter. Without the first, acme's `invoice-followup` resolves to
whichever boundary is active anywhere; without the second, a busy tenant
exhausts a quiet one's allowance and neighbourly load becomes a denial of
service.

The leak this is really guarding against is a credential one — tenant A's
boundary quietly borrowing tenant B's Gmail token — so it is enforced in the
database, on **both** sides of the reference:

- a boundary may not name a credential in another tenant, or one that
  doesn't exist;
- a credential may not move to another tenant while boundaries still point
  at it.

One trigger without the other is not a rule. Guarding only the boundary
leaves the credential free to walk out from under it, and every boundary
still holding the reference becomes cross-tenant with nothing raised.

`correlation_id` is the other half: one thread of work across many actions.
An `interpret()` run stamps every proposal it makes with a single id, so the
log replays a whole episode of reasoning — including the denies the model
adapted around — instead of a scatter of unrelated rows. It is nullable; a
one-off action has no thread, and pretending otherwise would be a lie in
the log.

## The control room

`scripts/control_room.py` serves a three-pane GUI on `127.0.0.1:8400`
(`pip install bounds[gui]`): the **boundary** (what is granted — read-only,
deliberately: boundaries change in psql, and a GUI that can widen a boundary
is a GUI that can be talked into it), a **proposal box** (what is being
asked — a natural-language prompt through `interpret()`, or a direct
proposal straight into `dispatch()`; both land on the same gate), and the
**action log** (what was decided, live). The GUI adds no authority and holds
no state; it is a window onto the gate, not a surface over it.

## Getting started

Needs Python 3.11+ and Postgres (17 is what it's tested on).

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                          # unit tests; no database needed
```

To run the pipeline end to end against a real database:

```bash
docker run -d --name bounds-pg -e POSTGRES_PASSWORD=bounds \
    -e POSTGRES_DB=bounds -p 127.0.0.1:5433:5432 postgres:17-alpine
for f in migrations/*.sql; do psql "postgresql://postgres:bounds@127.0.0.1:5433/bounds" -f "$f"; done
python scripts/live_smoke.py                    # allow, deny, replay, hold, reconcile, cross-tenant
```

Set `BOUNDS_DSN` to point the scripts at a different database. Extras:
`[interpret]` (the Anthropic SDK, for `interpret()` and
`scripts/live_interpret.py`), `[secrets]` (OS keyring), `[gui]` (the control
room: `python scripts/control_room.py`).

## Layout

| Path | What |
|---|---|
| `migrations/` | Plain SQL. No ORM, no migration framework yet. |
| `src/bounds/models.py` | `ActionRequest` / `Decision` — the two wire shapes. |
| `src/bounds/catalogue.py` | What an action *is*. Registry, param schema, limit vocabulary. |
| `src/bounds/check.py` | The boundary check. Pure but for one rate query. |
| `src/bounds/tools.py` | Boundary → the model's tool list. Offering, never granting. |
| `src/bounds/interpret.py` | NL trigger → model → gated proposals. The only place a model belongs. |
| `src/bounds/executor.py` | Log-then-dispatch. |
| `src/bounds/secrets.py` | Pointer → secret, at the moment of use. Core stays secret-free. |
| `src/bounds/gui.py` | The control room. A window onto the gate, never authority over it. |
| `tests/` | Emphasis on deny paths. |

## Explicit non-goals (for now)

- Multi-step / partially-executed actions. Single-step connectors only.
- Delegation chains, RBAC. (Single-verdict approvals exist — see Approvals.)
- Roles. `boundary.actions_allowed` is already the bundle; an agent→role→action
  indirection buys nothing until a third agent needs the same bundle, and costs
  a join in the gate.
- Trigger-aware boundaries. `trigger_type` is logged but not checked — a prompt
  and a cron job get identical scrutiny, deliberately, for now.
- Temporal, NATS, OPA. Postgres and a function are enough at this size.
- A boundary-authoring API. Boundaries go in by hand, in psql —
  `catalogue.validate_boundary()` is the pre-flight.

## Status

Pre-alpha. Nothing here has been run against a live connector.

The pipeline *is* proven end-to-end against a real Postgres —
`scripts/live_smoke.py` drives allow, deny, replay, hold/approve/reject,
crash-and-reconcile, and cross-tenant denial through the real gate, log, and
triggers, with a stub connector standing in for the downstream. The model
path is unproven live; it needs an `ANTHROPIC_API_KEY`.

`check()` evaluates `now` as an aware UTC datetime; `hours_allowed` is
therefore interpreted in UTC.

## howldr

bounds is free to run yourself. It is one half of **Leaps and Bounds**, by
howldr; for supported deployments, ask at
[hello@howldr.com](mailto:hello@howldr.com).

## Licence

The code is [MIT-licensed](LICENSE) — © 2026 howldr Services Group, LLC. Use
it, change it, host it.

The names "howldr" and "Leaps and Bounds" and the howldr logo are not covered
by the MIT licence. See [TRADEMARKS.md](TRADEMARKS.md) for how to name a fork
or a hosted offering. To report a vulnerability, see [SECURITY.md](SECURITY.md).
