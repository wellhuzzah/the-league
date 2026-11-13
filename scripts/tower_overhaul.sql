-- The Tower overhaul (2026-11-12): daily cache table, keep-best scores. Run as postgres:
--   psql -h localhost -U postgres -d rockwood -v ON_ERROR_STOP=1 -f scripts\tower_overhaul.sql
-- Recorded in schema_v3.sql section 9. Undo: scripts\rollback_tower_overhaul.sql.

BEGIN;

-- Daily cache: the first GET /tower/daily of a UTC date stores the round; every later one reads it.
CREATE TABLE tower_daily (
	daily_seed  DATE PRIMARY KEY,
	questions   JSONB NOT NULL,
	created_at  TIMESTAMPTZ DEFAULT now()
);
GRANT SELECT, INSERT ON tower_daily TO rockwood_api;
-- rockwood_backup reads it through pg_read_all_data; no grant needed.

-- Keep-best: one row per player per board. Dedupe first (keeps the best score per key, the earliest
-- on equal scores); it matched no rows when this was written.
DELETE FROM tower_scores s USING tower_scores b
WHERE lower(trim(s.player_name)) = lower(trim(b.player_name))
  AND s.daily_seed IS NOT DISTINCT FROM b.daily_seed
  AND (b.score > s.score OR (b.score = s.score AND (b.played_at, b.id) < (s.played_at, s.id)));

CREATE UNIQUE INDEX tower_scores_best_daily ON tower_scores (lower(trim(player_name)), daily_seed)
	WHERE daily_seed IS NOT NULL;
CREATE UNIQUE INDEX tower_scores_best_free ON tower_scores (lower(trim(player_name)))
	WHERE daily_seed IS NULL;
GRANT UPDATE ON tower_scores TO rockwood_api;   -- ON CONFLICT DO UPDATE

COMMIT;
