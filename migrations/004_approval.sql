-- 004_approval.sql — hold: the third decision, and the humans who resolve it.
-- Apply with: psql -d bounds -f migrations/004_approval.sql

BEGIN;

-- A boundary may demand a named human's approval before anything under it
-- executes. The check stays deterministic: approval_required and no approval
-- yet means hold, every time.
ALTER TABLE boundary ADD COLUMN approval_required boolean NOT NULL DEFAULT false;

-- The log now records the model tier the request claimed, so a held request
-- can be re-checked at release time from the log alone.
ALTER TABLE action_log ADD COLUMN model_tier_used text NOT NULL DEFAULT 'local';

-- 'hold' is a would-be allow waiting on a human. Everything that would deny
-- still denies — only requests worth a human's attention are held.
ALTER TABLE action_log DROP CONSTRAINT decision_valid;
ALTER TABLE action_log ADD CONSTRAINT decision_valid
    CHECK (decision IN ('allow', 'deny', 'hold'));

-- deny needs a reason; hold may carry one (informative), allow never does.
ALTER TABLE action_log DROP CONSTRAINT deny_has_reason;
ALTER TABLE action_log ADD CONSTRAINT deny_has_reason
    CHECK (decision <> 'deny' OR deny_reason IS NOT NULL);

-- deny_no_outcome already reads: decision = 'allow' OR outcome IS NULL.
-- That covers hold too — a held row has no outcome until released.

-- Who approved, exactly one verdict per held row, append-only.
CREATE TABLE approval (
    id            bigserial   PRIMARY KEY,
    action_log_id bigint      NOT NULL UNIQUE REFERENCES action_log(id),
    verdict       text        NOT NULL,
    approved_by   text        NOT NULL,   -- human identity, never an agent
    reason        text,
    created_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT verdict_valid CHECK (verdict IN ('approved', 'rejected'))
);

-- An approval can only target a held row: approving a deny would be an
-- override path, and there is no override path.
CREATE OR REPLACE FUNCTION approval_targets_hold() RETURNS trigger AS $$
BEGIN
    IF (SELECT decision FROM action_log WHERE id = NEW.action_log_id) IS DISTINCT FROM 'hold' THEN
        RAISE EXCEPTION 'log % is not held — approvals only attach to hold rows',
            NEW.action_log_id;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER approval_targets_hold
    BEFORE INSERT ON approval
    FOR EACH ROW EXECUTE FUNCTION approval_targets_hold();

-- Decisions are final except hold, which may become allow or deny — and only
-- with an approval row standing behind the transition. allow additionally
-- requires the verdict to be 'approved'. (deny with an 'approved' verdict is
-- legitimate: the release re-check can still deny if limits have moved.)
CREATE OR REPLACE FUNCTION action_log_decision_guard() RETURNS trigger AS $$
DECLARE
    a approval%ROWTYPE;
BEGIN
    IF NEW.decision = OLD.decision THEN
        RETURN NEW;
    END IF;
    IF OLD.decision <> 'hold' THEN
        RAISE EXCEPTION 'decision % on log % is final', OLD.decision, OLD.id;
    END IF;
    SELECT * INTO a FROM approval WHERE action_log_id = OLD.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'log % has no approval — a hold only moves on a human''s verdict', OLD.id;
    END IF;
    IF NEW.decision = 'allow' AND a.verdict <> 'approved' THEN
        RAISE EXCEPTION 'log % verdict is %, cannot become allow', OLD.id, a.verdict;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER action_log_decision_guard
    BEFORE UPDATE OF decision ON action_log
    FOR EACH ROW EXECUTE FUNCTION action_log_decision_guard();

COMMIT;
