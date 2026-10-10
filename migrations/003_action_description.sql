-- 003_action_description.sql — actions carry the text the model triggers on.
-- Apply with: psql -d bounds -f migrations/003_action_description.sql

BEGIN;

-- The tool list handed to a model is generated from boundary + action rows,
-- and a model decides WHEN to use a tool from its description. An action
-- without one is a capability nobody can reach for. Written by
-- bounds.catalogue.sync(), like the rest of this table; the DEFAULT exists
-- only so this migration applies cleanly to a table that already has rows.
ALTER TABLE action ADD COLUMN description text NOT NULL DEFAULT '';

COMMIT;
