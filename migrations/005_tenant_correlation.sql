-- 005_tenant_correlation.sql — whose action, and which thread of work.
-- Apply with: psql -d bounds -f migrations/005_tenant_correlation.sql

BEGIN;

-- Tenant: the isolation boundary above agents. Every table that names an
-- agent or a credential now names an owner. Existing rows land in
-- 'default', which stays the tenant for single-tenant deployments.
ALTER TABLE boundary   ADD COLUMN tenant text NOT NULL DEFAULT 'default';
ALTER TABLE credential ADD COLUMN tenant text NOT NULL DEFAULT 'default';
ALTER TABLE action_log ADD COLUMN tenant text NOT NULL DEFAULT 'default';

-- Correlation: one thread of work across many actions. An interpret() run
-- stamps every proposal it makes with one id; a workflow does the same.
-- Nullable — a one-off action has no thread.
ALTER TABLE action_log ADD COLUMN correlation_id text;

-- Uniqueness moves inside the tenant: acme's invoice-followup and
-- initech's invoice-followup are different agents with different rules.
DROP INDEX boundary_one_active;
CREATE UNIQUE INDEX boundary_one_active ON boundary (tenant, agent) WHERE active;
ALTER TABLE boundary DROP CONSTRAINT boundary_agent_version_key;
ALTER TABLE boundary ADD CONSTRAINT boundary_tenant_agent_version_key
    UNIQUE (tenant, agent, version);

-- The rate limit is per-tenant too — one tenant's sends must never
-- consume another's allowance.
DROP INDEX action_log_rate;
CREATE INDEX action_log_rate ON action_log (tenant, agent, action_requested, ts)
    WHERE decision = 'allow';

CREATE INDEX action_log_correlation ON action_log (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- A boundary may only reference a credential in its own tenant. Without
-- this, tenant A's boundary quietly borrows tenant B's Gmail token —
-- the exact cross-tenant leak the column exists to prevent.
CREATE OR REPLACE FUNCTION boundary_credential_same_tenant() RETURNS trigger AS $$
DECLARE
    cred_tenant text;
BEGIN
    SELECT tenant INTO cred_tenant FROM credential WHERE ref = NEW.credential_ref;
    IF cred_tenant IS NULL THEN
        RAISE EXCEPTION 'credential % does not exist', NEW.credential_ref;
    END IF;
    IF cred_tenant <> NEW.tenant THEN
        RAISE EXCEPTION 'boundary in tenant % may not use credential % (tenant %)',
            NEW.tenant, NEW.credential_ref, cred_tenant;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER boundary_credential_same_tenant
    BEFORE INSERT OR UPDATE OF credential_ref, tenant ON boundary
    FOR EACH ROW EXECUTE FUNCTION boundary_credential_same_tenant();

-- The same rule from the other side. Guarding only the boundary leaves the
-- credential free to walk out from under it: move a credential to another
-- tenant and every boundary still pointing at it is now cross-tenant, with
-- nothing raised. A rule enforced on one side of a reference isn't enforced.
CREATE OR REPLACE FUNCTION credential_tenant_no_orphan() RETURNS trigger AS $$
DECLARE
    stranded text;
BEGIN
    SELECT string_agg(agent, ', ') INTO stranded
    FROM boundary WHERE credential_ref = NEW.ref AND tenant <> NEW.tenant;
    IF stranded IS NOT NULL THEN
        RAISE EXCEPTION 'credential % cannot move to tenant %: boundaries still use it (%)',
            NEW.ref, NEW.tenant, stranded;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER credential_tenant_no_orphan
    BEFORE UPDATE OF tenant ON credential
    FOR EACH ROW EXECUTE FUNCTION credential_tenant_no_orphan();

COMMIT;
