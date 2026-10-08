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
-- 2. transactions + transaction_items   (PENDING — not applied yet)
-- ===========================================================================
-- One row per ESPN transaction (WAIVER, FREEAGENT, or ROSTER with a DROP), and
-- one row per player moved in it. Loaded by espn_transactions_import.py.
-- Event time: COALESCE(processed_at, proposed_at). processDate exists only on
-- waiver rows; both dates are stored exactly as ESPN gives them (2018 rounds
-- processDate to the hour, so it can be earlier than proposedDate).
CREATE TABLE transactions (
	id                  SERIAL PRIMARY KEY,
	espn_txn_id         UUID NOT NULL UNIQUE,
	espn_related_txn_id UUID,                  -- relatedTransactionId as given by ESPN
	cancel_twin_of      UUID,                  -- set on a system CANCEL row that duplicates a failed
	                                           -- claim (2018: matched on items); = that claim's espn_txn_id
	season              NUMERIC(4,0) NOT NULL,
	week                INTEGER NOT NULL,      -- the transaction's scoringPeriodId
	team_id             INTEGER NOT NULL REFERENCES teams(team_id),  -- from the items, not top-level teamId
	type                VARCHAR(16) NOT NULL,  -- WAIVER / FREEAGENT / ROSTER
	status              VARCHAR(48) NOT NULL,  -- EXECUTED, CANCELED, FAILED_* (open list)
	bid_amount          INTEGER,
	proposed_at         TIMESTAMPTZ NOT NULL,
	processed_at        TIMESTAMPTZ
);
CREATE INDEX idx_transactions_season_week ON transactions (season, week);
CREATE INDEX idx_transactions_team        ON transactions (team_id);

CREATE TABLE transaction_items (
	id               SERIAL PRIMARY KEY,
	transaction_id   INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
	item_type        VARCHAR(4) NOT NULL CHECK (item_type IN ('ADD', 'DROP')),
	espn_player_id   INTEGER NOT NULL,        -- negative = D/ST
	player_name      VARCHAR,                 -- NULL if ESPN's player list had no name
	position         VARCHAR,
	from_team_id     INTEGER REFERENCES teams(team_id),  -- NULL = free agent pool (ESPN 0 / -1)
	to_team_id       INTEGER REFERENCES teams(team_id),  -- NULL = free agent pool
	UNIQUE (transaction_id, item_type, espn_player_id)
);
CREATE INDEX idx_transaction_items_player ON transaction_items (espn_player_id);
