-- schema_v3.sql — Rockwood schema changes after v2.
-- Applied by hand with psql or an asyncpg one-off, in the order below.

-- ===========================================================================
-- 1. box_scores: full rosters, real lineup slot ids, has_stats   (2026-10-07)
-- ===========================================================================

-- 1a. backup (also: pg_dump -t box_scores -> backups\box_scores_20261007.sql)
CREATE TABLE box_scores_backup_20261007 AS SELECT * FROM box_scores;

-- 1b. lineup_slot becomes ESPN's lineupSlotId
--     (0 QB, 2 RB, 4 WR, 6 TE, 16 D/ST, 17 K, 23 FLEX, 20 bench, 21 IR).
--     Old values were the constant 'QB' (2018-2025) or slot labels (2026); nothing
--     read the column. espn_import.py --box-scores-only refills every row.
ALTER TABLE box_scores ALTER COLUMN lineup_slot TYPE SMALLINT USING NULL;

-- 1c. has_stats: FALSE when ESPN has no actual stat line for the player that
--     week (NFL bye or inactive); points_scored is then 0.
ALTER TABLE box_scores ADD COLUMN has_stats BOOLEAN;

-- 1d. after the re-import has filled every row
ALTER TABLE box_scores ALTER COLUMN lineup_slot SET NOT NULL;
ALTER TABLE box_scores ADD CONSTRAINT box_scores_starter_from_slot
	CHECK (is_starter = (lineup_slot NOT IN (20, 21)));

-- ===========================================================================
-- 2. transactions   (PENDING — not applied yet)
-- ===========================================================================
-- CREATE TABLE transactions (
-- 	id                  SERIAL PRIMARY KEY,
-- 	espn_txn_id         UUID NOT NULL UNIQUE,
-- 	espn_related_txn_id UUID,
-- 	season              NUMERIC(4,0) NOT NULL,
-- 	week                INTEGER NOT NULL,
-- 	team_id             INTEGER NOT NULL REFERENCES teams(team_id),
-- 	type                VARCHAR(16) NOT NULL,
-- 	status              VARCHAR(48) NOT NULL,
-- 	bid_amount          INTEGER,
-- 	proposed_at         TIMESTAMPTZ NOT NULL,
-- 	processed_at        TIMESTAMPTZ,
-- 	add_player_id       INTEGER,
-- 	add_player_name     VARCHAR,
-- 	add_position        VARCHAR,
-- 	drop_player_id      INTEGER,
-- 	drop_player_name    VARCHAR,
-- 	drop_position       VARCHAR,
-- 	CHECK (add_player_id IS NOT NULL OR drop_player_id IS NOT NULL)
-- );
-- CREATE INDEX idx_transactions_season_week ON transactions (season, week);
-- CREATE INDEX idx_transactions_team        ON transactions (team_id);
-- CREATE INDEX idx_transactions_add_player  ON transactions (add_player_id);
