-- 001_init.sql — boundaries, scoped credentials, action log.
-- Apply with: psql -d bounds -f migrations/001_init.sql

BEGIN;

-- Boundaries: pre-declared limits. Versioned, never updated in place, so the
-- log can point at exactly the rule that was checked.
CREATE TABLE boundary (
    id              bigserial PRIMARY KEY,
    agent           text        NOT NULL,
    version         int         NOT NULL,
    data_scope      text[]      NOT NULL,   -- ['invoices.read','contacts.read']
    actions_allowed text[]      NOT NULL,   -- ['send_email']
    limits          jsonb       NOT NULL DEFAULT '{}',
    model_tier      text        NOT NULL,   -- 'local' | 'cloud'
    credential_ref  text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL,   -- human identity, never an agent
    active          boolean     NOT NULL DEFAULT true,

    CONSTRAINT model_tier_valid CHECK (model_tier IN ('local','cloud')),
    UNIQUE (agent, version)
);

-- Exactly one active version per agent.
CREATE UNIQUE INDEX boundary_one_active ON boundary (agent) WHERE active;

-- Scoped credentials. The executor never holds a general secret. `scope` must
-- be a subset of the boundary's actions_allowed — this table is the thing that
-- makes the limit physical rather than advisory.
CREATE TABLE credential (
    ref         text PRIMARY KEY,       -- 'gmail-send-only-invoicebot'
    connector   text        NOT NULL,   -- 'gmail'
    scope       text[]      NOT NULL,   -- ['send_email']
    secret_uri  text        NOT NULL,   -- pointer into keyring/vault, NOT the secret
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Append-only. Also the rate-limit source of truth — don't build a second
-- counter you have to keep in sync.
CREATE TABLE action_log (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    idempotency_key   text        NOT NULL,
    trigger_type      text        NOT NULL,   -- 'scheduled'|'event'|'nl'
    trigger_source    text        NOT NULL,
    requested_by      text        NOT NULL,
    agent             text        NOT NULL,
    boundary_version  int,                    -- null iff no boundary matched
    action_requested  text        NOT NULL,
    action_params     jsonb       NOT NULL DEFAULT '{}',
    decision          text        NOT NULL,   -- 'allow'|'deny'
    deny_reason       text,
    outcome           text,                   -- 'success'|'error'|null if denied
    result            jsonb,
    data_touched      text[]      NOT NULL DEFAULT '{}',

    CONSTRAINT decision_valid   CHECK (decision IN ('allow','deny')),
    CONSTRAINT outcome_valid    CHECK (outcome IS NULL OR outcome IN ('success','error')),
    CONSTRAINT deny_has_reason  CHECK (decision = 'allow' OR deny_reason IS NOT NULL),
    CONSTRAINT deny_no_outcome  CHECK (decision = 'allow' OR outcome IS NULL),
    -- replay protection: a retried trigger collides here instead of double-sending
    UNIQUE (idempotency_key)
);

CREATE INDEX action_log_rate ON action_log (agent, action_requested, ts)
    WHERE decision = 'allow';

COMMIT;
