-- espn_player_id NOT NULL on draft_picks and box_scores (2026-11-13, clickable player names phase 1).
-- The site links every player name by id, so every row must have one. Both tables had 0 nulls;
-- transaction_items already forbids them. espn_import.py skips (with a warning) any ESPN roster entry
-- or draft pick that has no playerId, so the weekly import can't trip this.
-- Run as postgres:
--   psql -h localhost -U postgres -d rockwood -v ON_ERROR_STOP=1 -f scripts\player_id_not_null.sql
-- Undo: scripts\rollback_player_id_not_null.sql
BEGIN;
ALTER TABLE draft_picks ALTER COLUMN espn_player_id SET NOT NULL;
ALTER TABLE box_scores  ALTER COLUMN espn_player_id SET NOT NULL;
COMMIT;
