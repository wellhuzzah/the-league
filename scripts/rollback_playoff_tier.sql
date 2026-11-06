-- Reverses schema_v3.sql section 7 (matchups.playoff_tier) on its own, leaving the rest of Phase 3
-- in place. Run as postgres, after reverting the luck commit (the new luck query reads the column):
--   psql -h localhost -U postgres -d rockwood -X -f scripts\rollback_playoff_tier.sql
-- team_games is rebuilt without the column: CREATE OR REPLACE can't drop a column, so the view is
-- dropped and created again; it gets its SELECT grants back from the default privileges.
\set ON_ERROR_STOP on
BEGIN;

DROP VIEW team_games;

CREATE VIEW team_games AS
SELECT m.id AS matchup_id, m.season, m.week, m.is_playoffs,
       m.home_team_id AS team_id, m.away_team_id AS opp_id,
       m.home_score AS score, m.away_score AS opp_score, m.margin,
       CASE WHEN m.winner_team_id IS NULL THEN 'T'
            WHEN m.winner_team_id = m.home_team_id THEN 'W' ELSE 'L' END AS result
FROM matchups m
UNION ALL
SELECT m.id, m.season, m.week, m.is_playoffs,
       m.away_team_id, m.home_team_id,
       m.away_score, m.home_score, m.margin,
       CASE WHEN m.winner_team_id IS NULL THEN 'T'
            WHEN m.winner_team_id = m.away_team_id THEN 'W' ELSE 'L' END
FROM matchups m;

ALTER TABLE matchups DROP COLUMN playoff_tier;

COMMIT;

SELECT grantee, privilege_type FROM information_schema.role_table_grants
WHERE table_name = 'team_games' AND grantee LIKE 'rockwood%' ORDER BY grantee;
