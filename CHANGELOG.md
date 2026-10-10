# Changelog

Notable changes to bounds. Versions follow
[Semantic Versioning](https://semver.org/); while the version starts with `0.`,
any release may change behaviour.

## [0.1.0] — 2026-10-10

First public release, under the MIT licence. Pre-alpha: proven end-to-end
against a real Postgres with a stub connector; not yet run against a live
connector.

### The pipeline

- **Boundary check** — default-deny, deterministic `check()`: no model takes
  part in the decision to allow.
- **Action catalogue** — actions are registered callables with a declared param
  schema; limits bind to params through `feeds`, and a limit nothing feeds is a
  deny. Registry/database drift refuses to boot.
- **Tool list** — the model's tool menu is generated from the agent's active
  boundary, with limits stated in each description.
- **Interpret** — a natural-language trigger runs through an injected Anthropic
  client; every proposal goes through the same gate and log.
- **Executor** — decide, commit, then execute; the log is the rate limiter.
- **Secrets** — credentials are pointers (`env:`, `keyring://`) dereferenced at
  the moment of use; the log holds only the reference.
- **Approvals** — `approval_required` holds would-be allows for a named human;
  release re-checks as of now. State machine enforced by Postgres triggers.
- **Reconciliation** — closes "I don't know if this happened" rows by asking
  the downstream, and leaves them unknown when it can't say.
- **Tenants and correlation** — per-tenant boundaries, rate limits, and
  credential isolation enforced on both sides of the reference; one
  correlation id per interpret run.
- **Control room** — a local, read-only-boundary GUI onto the gate.
