-- 002_action_catalogue.sql — the list of things an agent can be granted.
-- Apply with: psql -d bounds -f migrations/002_action_catalogue.sql

BEGIN;

-- Written by bounds.catalogue.sync() from the Python registry, never by hand:
-- the schema has to describe the code that actually runs. It lives here so a
-- boundary authored in psql can be checked against it.
CREATE TABLE action (
    name          text PRIMARY KEY,       -- 'send_email'
    connector     text        NOT NULL,   -- 'gmail'
    params_schema jsonb       NOT NULL,   -- {"to": {"type":"list[str]","required":true,"feeds":"max_recipients"}}
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- `actions_allowed` is a text[], and Postgres has no per-element foreign key,
-- so the referential check is a trigger. Same intent as an FK: a boundary that
-- names an action nobody implemented fails while a human is still looking at
-- it. Without this it fails closed but silently — every action denied, no clue
-- why. Fail closed AND loud.
CREATE OR REPLACE FUNCTION boundary_actions_catalogued() RETURNS trigger AS $$
DECLARE
    missing text[];
BEGIN
    SELECT array_agg(a) INTO missing
    FROM unnest(NEW.actions_allowed) AS a
    WHERE NOT EXISTS (SELECT 1 FROM action WHERE name = a);

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'actions_allowed names uncatalogued action(s): %', missing
            USING HINT = 'run bounds.catalogue.sync(), or fix the typo';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER boundary_actions_catalogued
    BEFORE INSERT OR UPDATE OF actions_allowed ON boundary
    FOR EACH ROW EXECUTE FUNCTION boundary_actions_catalogued();

-- The other direction: an action can't be dropped while a live boundary grants
-- it. Deactivate the boundary first — that's a deliberate, versioned change.
CREATE OR REPLACE FUNCTION action_not_granted() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM boundary
        WHERE active AND OLD.name = ANY (actions_allowed)
    ) THEN
        RAISE EXCEPTION 'action % is granted by an active boundary', OLD.name;
    END IF;
    RETURN OLD;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER action_not_granted
    BEFORE DELETE ON action
    FOR EACH ROW EXECUTE FUNCTION action_not_granted();

COMMIT;
