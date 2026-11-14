-- NFL teams for players (2026-11-13, NFL team data phase 1). Run as postgres:
--   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -h localhost -U postgres -d rockwood -v ON_ERROR_STOP=1 -f scripts\nfl_teams.sql
-- Recorded in schema_v3.sql section 11. Undo: scripts\rollback_nfl_teams.sql.
-- nfl_team_import.py fills all three tables (rockwood_writer); SELECT reaches rockwood_api and
-- rockwood_writer through the default privileges, and rockwood_backup through pg_read_all_data.

BEGIN;

-- One row per NFL team per season. Abbreviations come from that season: ESPN's ids survive
-- relocations (13 = OAK to 2019, then LV; 14 = STL to 2015, then LAR; 24 = SD to 2016, then LAC).
CREATE TABLE nfl_teams (
	season       SMALLINT    NOT NULL,
	pro_team_id  SMALLINT    NOT NULL,      -- ESPN proTeamId
	abbrev       VARCHAR(4)  NOT NULL,      -- upper case, ESPN style (WSH, JAX, LAR)
	location     VARCHAR(40) NOT NULL,
	name         VARCHAR(40) NOT NULL,      -- nickname that season
	bye_week     SMALLINT,                  -- from ESPN, 2018 on; NULL before
	PRIMARY KEY (season, pro_team_id)
);

-- The player's NFL team in a given week. A missing row means unknown, never a guess.
-- source: espn     = ESPN's actual stat line for that week, and the player played
--         nflverse = nflverse weekly roster (active, reserve, inactive, PUP, suspended...)
--         gap_fill = no record that week; the nearest recorded weeks before and after agree
--         bye      = no record that week, and the team of the nearest recorded week had no game
--                    in any week between (a bye or a postponed game next to a trade or the edge)
--         dst_id   = a D/ST: the team is -(espn_player_id) - 16000
CREATE TABLE player_team_weeks (
	espn_player_id  INTEGER     NOT NULL,   -- negative = D/ST
	season          SMALLINT    NOT NULL,
	week            SMALLINT    NOT NULL,
	pro_team_id     SMALLINT    NOT NULL,
	source          VARCHAR(10) NOT NULL CHECK (source IN ('espn', 'nflverse', 'gap_fill', 'bye', 'dst_id')),
	PRIMARY KEY (espn_player_id, season, week),
	FOREIGN KEY (season, pro_team_id) REFERENCES nfl_teams
);
CREATE INDEX idx_player_team_weeks_season_week ON player_team_weeks (season, week);

-- One row per player the league has ever touched who is in ESPN's current-season player pool:
-- the player's team now. pro_team_id NULL = ESPN lists no team (free agent or retired).
CREATE TABLE players (
	espn_player_id  INTEGER     PRIMARY KEY,
	pro_team_id     SMALLINT,
	team_season     SMALLINT    NOT NULL,   -- the season of the pool file it came from
	updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
	FOREIGN KEY (team_season, pro_team_id) REFERENCES nfl_teams
);

GRANT INSERT, UPDATE, DELETE ON nfl_teams, player_team_weeks, players TO rockwood_writer;

COMMIT;
