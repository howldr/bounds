# Security policy

bounds is a gate: its job is to say no. Anything that makes it say yes when it
shouldn't — or lets an action happen without the gate, the log, or the scoped
credential — is a security issue and in scope.

## Supported versions

Only the latest commit on `main` is supported. Fixes are not backported.

## Reporting a vulnerability

Please report privately, not in a public issue:

- **GitHub:** use **Security → Report a vulnerability** on this repository, or
- **Email:** [hello@howldr.com](mailto:hello@howldr.com) with "SECURITY" in the subject.

Include what you found, how to reproduce it, and the impact you expect. We'll
acknowledge reports as soon as we can and keep you updated until the issue is
resolved. Please give us a reasonable chance to fix the issue before disclosing
it publicly. We're glad to credit you in the fix unless you'd rather stay
anonymous.

## In scope

- An allow without a matching active boundary, or a request that passes
  `check()` while exceeding a declared limit (including params shaped so a
  limit reads as zero)
- Executing an action without a committed `action_log` row, or bypassing the
  `max_per_day` count
- Cross-tenant access — a boundary or request reaching another tenant's
  credential, boundary, or rate allowance
- Moving a held request to allow without a standing approval, or changing a
  final allow/deny decision
- A secret value reaching the log, a receipt, the tool list, or the model
- Anything that lets model output change what is permitted, rather than what
  is proposed

## Out of scope

- Vulnerabilities in Postgres, the OS keyring, or a connector's downstream
  service — report those to the vendor
- Findings that need direct write access to the database or an already
  compromised host (boundaries are authored in psql by design)
- The control room bound to anything other than `127.0.0.1` — it is a local
  operator tool with no authentication
- Missing hardening or best-practice notes with no demonstrated impact
