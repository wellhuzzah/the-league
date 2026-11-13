-- Undo scripts\player_id_not_null.sql. Run as postgres:
--   psql -h localhost -U postgres -d rockwood -v ON_ERROR_STOP=1 -f scripts\rollback_player_id_not_null.sql
BEGIN;
ALTER TABLE draft_picks ALTER COLUMN espn_player_id DROP NOT NULL;
ALTER TABLE box_scores  ALTER COLUMN espn_player_id DROP NOT NULL;
COMMIT;
