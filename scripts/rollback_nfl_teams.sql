-- Undo scripts\nfl_teams.sql. Run as postgres. No other table refers to these; the API and the
-- weekly job must be on code that doesn't read them (revert nfl_team_import.py's step in
-- weekly_maintenance.py first).
BEGIN;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS player_team_weeks;
DROP TABLE IF EXISTS nfl_teams;
COMMIT;
