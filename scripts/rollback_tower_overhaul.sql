-- Undo scripts\tower_overhaul.sql. Run as postgres (rollback_tower_overhaul.ps1 does).
-- Scores stay as they are (one row per player per board); the old code only inserts.
BEGIN;
DROP TABLE IF EXISTS tower_daily;
DROP INDEX IF EXISTS tower_scores_best_daily;
DROP INDEX IF EXISTS tower_scores_best_free;
REVOKE UPDATE ON tower_scores FROM rockwood_api;
COMMIT;
