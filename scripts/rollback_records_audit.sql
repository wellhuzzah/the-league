-- Reverses scripts\records_audit_fix.sql: every matchups and records value it changed goes back,
-- then schema_v3.sql section 8 is undone. Run as postgres, after reverting the code commits that read
-- the adjustment columns:
--   psql -h localhost -U postgres -d rockwood -X -f scripts\rollback_records_audit.sql
-- Each UPDATE checks the row is as the fix left it. team_games is rebuilt without the new columns:
-- CREATE OR REPLACE can't drop a column, so the view is dropped and created again (as in section 7);
-- it gets its SELECT grants back from the default privileges.
-- Or restore the dump taken first: pg_restore -h localhost -U postgres -d rockwood --clean -t matchups -t records ...
\set ON_ERROR_STOP on
BEGIN;

DO $$
BEGIN
  UPDATE matchups SET home_score = 88.00, away_score = 88.00, winner_team_id = NULL
   WHERE id = 1350 AND home_score = 88.00 AND away_score = 88.00 AND winner_team_id IS NOT DISTINCT FROM 11;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1350 (2009 W1 joey r v mike o) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 91.00, away_score = 91.00, winner_team_id = NULL
   WHERE id = 1400 AND home_score = 91.00 AND away_score = 91.00 AND winner_team_id IS NOT DISTINCT FROM 3;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1400 (2009 W10 nick o v josh d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 99.00, away_score = 99.00, winner_team_id = NULL
   WHERE id = 1416 AND home_score = 99.00 AND away_score = 99.00 AND winner_team_id IS NOT DISTINCT FROM 12;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1416 (2009 W12 dan t v joey r) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 84.00, away_score = 80.00, winner_team_id = 8
   WHERE id = 1443 AND home_score = 84.00 AND away_score = 81.00 AND winner_team_id IS NOT DISTINCT FROM 8;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1443 (2010 W1 ryan l v dan t) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 101.00, away_score = 101.00, winner_team_id = NULL
   WHERE id = 1589 AND home_score = 101.00 AND away_score = 101.00 AND winner_team_id IS NOT DISTINCT FROM 13;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1589 (2011 W8 dan t v ryan j) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 56.00, away_score = 55.00, winner_team_id = 13
   WHERE id = 1595 AND home_score = 56.00 AND away_score = 57.00 AND winner_team_id IS NOT DISTINCT FROM 11;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1595 (2011 W9 ryan j v mike o) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 80.00, away_score = 69.00, winner_team_id = 2
   WHERE id = 1654 AND home_score = 86.00 AND away_score = 69.00 AND winner_team_id IS NOT DISTINCT FROM 2;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1654 (2012 W2 joey j v josh d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 87.00, away_score = 87.00, winner_team_id = NULL
   WHERE id = 1688 AND home_score = 87.00 AND away_score = 87.00 AND winner_team_id IS NOT DISTINCT FROM 7;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1688 (2012 W6 josh d v greg d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 73.00, away_score = 73.00, winner_team_id = NULL
   WHERE id = 1726 AND home_score = 73.00 AND away_score = 73.00 AND winner_team_id IS NOT DISTINCT FROM 7;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1726 (2012 W12 greg d v jake d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 90.00, away_score = 90.00, winner_team_id = NULL
   WHERE id = 1803 AND home_score = 90.00 AND away_score = 90.00 AND winner_team_id IS NOT DISTINCT FROM 3;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1803 (2013 W7 dan t v nick o) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 84.00, away_score = 84.00, winner_team_id = NULL
   WHERE id = 1817 AND home_score = 84.00 AND away_score = 84.00 AND winner_team_id IS NOT DISTINCT FROM 15;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1817 (2013 W9 kenny d v greg d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 87.00, away_score = 87.00, winner_team_id = NULL
   WHERE id = 1888 AND home_score = 87.00 AND away_score = 87.00 AND winner_team_id IS NOT DISTINCT FROM 4;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1888 (2014 W3 josh d v joey r) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 87.00, away_score = 87.00, winner_team_id = NULL
   WHERE id = 1958 AND home_score = 87.00 AND away_score = 87.00 AND winner_team_id IS NOT DISTINCT FROM 5;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1958 (2014 W13 ryan l v ben m) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 97.00, away_score = 97.00, winner_team_id = NULL
   WHERE id = 2021 AND home_score = 97.00 AND away_score = 97.00 AND winner_team_id IS NOT DISTINCT FROM 8;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2021 (2015 W6 ryan l v kenny d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 94.50, away_score = 94.50, winner_team_id = NULL
   WHERE id = 2121 AND home_score = 94.50 AND away_score = 94.50 AND winner_team_id IS NOT DISTINCT FROM 11;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2121 (2016 W4 brian h v mike o) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 102.50, away_score = 102.50, winner_team_id = NULL
   WHERE id = 2202 AND home_score = 102.50 AND away_score = 102.50 AND winner_team_id IS NOT DISTINCT FROM 11;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2202 (2016 W16 mike o v dan t) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 122.00, away_score = 122.00, winner_team_id = NULL
   WHERE id = 2334 AND home_score = 122.00 AND away_score = 122.00 AND winner_team_id IS NOT DISTINCT FROM 6;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2334 (2018 W3 brian h v owens) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 113.00, away_score = 113.00, winner_team_id = NULL
   WHERE id = 2338 AND home_score = 113.00 AND away_score = 113.00 AND winner_team_id IS NOT DISTINCT FROM 13;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2338 (2018 W3 ryan j v greg d) is not as the fix left it'; END IF;
  UPDATE matchups SET home_score = 110.50, away_score = 110.50, winner_team_id = NULL
   WHERE id = 3097 AND home_score = 110.50 AND away_score = 110.50 AND winner_team_id IS NOT DISTINCT FROM 14;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 3097 (2024 W17 brian h v joey r) is not as the fix left it'; END IF;

  UPDATE records SET wins = 4, losses = 12 WHERE id = 24 AND wins = 2 AND losses = 11 AND draws = 0 AND points_for = 995.00 AND points_against = 1169.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 24 (2010 josh s) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1176.0, points_against = 1139.0 WHERE id = 79 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1237.00 AND points_against = 1111.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 79 (2014 brian h) is not as the fix left it'; END IF;
  UPDATE records SET wins = 8, losses = 5 WHERE id = 96 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1232.00 AND points_against = 1367.50 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 96 (2016 joey j) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1225.5, points_against = 1209.0 WHERE id = 232 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1359.00 AND points_against = 1345.50 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 232 (2024 jake d) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1219.5, points_against = 1242.5 WHERE id = 229 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1328.00 AND points_against = 1332.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 229 (2024 joey j) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1279.5, points_against = 1223.5 WHERE id = 224 AND wins = 6 AND losses = 6 AND draws = 1 AND points_for = 1361.50 AND points_against = 1309.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 224 (2024 nick o) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1229.5, points_against = 1180.5 WHERE id = 226 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1366.00 AND points_against = 1314.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 226 (2024 josh d) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1184.5, points_against = 1269.5 WHERE id = 233 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1319.00 AND points_against = 1364.50 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 233 (2024 ben m) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1183.0, points_against = 1201.5 WHERE id = 228 AND wins = 6 AND losses = 6 AND draws = 1 AND points_for = 1269.00 AND points_against = 1354.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 228 (2024 owens) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1180.5, points_against = 1234.0 WHERE id = 223 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1287.00 AND points_against = 1293.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 223 (2024 greg d) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1095.0, points_against = 1283.5 WHERE id = 230 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1190.00 AND points_against = 1418.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 230 (2024 ryan l) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1282.0, points_against = 1156.5 WHERE id = 225 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1403.50 AND points_against = 1249.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 225 (2024 kevin m) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1326.0, points_against = 1108.5 WHERE id = 222 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1415.50 AND points_against = 1217.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 222 (2024 joey r) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1333.0, points_against = 1339.0 WHERE id = 227 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1418.50 AND points_against = 1421.00 AND most_points = true;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 227 (2024 mike o) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1115.0, points_against = 1042.5 WHERE id = 234 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1174.00 AND points_against = 1149.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 234 (2024 dan t) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1193.0, points_against = 1309.5 WHERE id = 231 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1285.50 AND points_against = 1431.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 231 (2024 ryan j) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1260.5, points_against = 1306.5 WHERE id = 221 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1413.00 AND points_against = 1392.50 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 221 (2024 brian h) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1164.9, points_against = 1308.2 WHERE id = 220 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1240.10 AND points_against = 1385.60 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 220 (2025 jake d) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1252.6, points_against = 1285.9 WHERE id = 218 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1344.70 AND points_against = 1390.90 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 218 (2025 joey j) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1274.3, points_against = 1369.0 WHERE id = 217 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1398.30 AND points_against = 1458.20 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 217 (2025 nick o) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1234.3, points_against = 1158.6 WHERE id = 208 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1311.70 AND points_against = 1233.80 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 208 (2025 josh d) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1462.4, points_against = 1339.3, most_points = false WHERE id = 219 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1561.50 AND points_against = 1456.00 AND most_points = true;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 219 (2025 ben m) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1142.5, points_against = 1311.5 WHERE id = 214 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1211.90 AND points_against = 1426.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 214 (2025 owens) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1475.3, points_against = 1258.4, most_points = true WHERE id = 211 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1554.00 AND points_against = 1381.00 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 211 (2025 greg d) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1190.1, points_against = 1243.6 WHERE id = 210 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1306.80 AND points_against = 1342.70 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 210 (2025 ryan l) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1298.9, points_against = 1236.9 WHERE id = 212 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1399.80 AND points_against = 1324.10 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 212 (2025 kevin m) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1138.5, points_against = 1254.4 WHERE id = 213 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1243.50 AND points_against = 1346.50 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 213 (2025 joey r) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1332.7, points_against = 1253.6 WHERE id = 209 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1421.90 AND points_against = 1377.60 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 209 (2025 mike o) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1161.1, points_against = 1142.1 WHERE id = 215 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1283.70 AND points_against = 1220.80 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 215 (2025 dan t) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1177.2, points_against = 1307.9 WHERE id = 216 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1264.40 AND points_against = 1408.80 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 216 (2025 ryan j) is not as the fix left it'; END IF;
  UPDATE records SET points_for = 1386.3, points_against = 1221.7 WHERE id = 207 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1500.80 AND points_against = 1291.10 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 207 (2025 brian h) is not as the fix left it'; END IF;
END $$;

DROP VIEW team_games;

CREATE VIEW team_games AS
SELECT m.id AS matchup_id, m.season, m.week, m.is_playoffs,
       m.home_team_id AS team_id, m.away_team_id AS opp_id,
       m.home_score AS score, m.away_score AS opp_score, m.margin,
       CASE WHEN m.winner_team_id IS NULL THEN 'T'
            WHEN m.winner_team_id = m.home_team_id THEN 'W' ELSE 'L' END AS result,
       m.playoff_tier
FROM matchups m
UNION ALL
SELECT m.id, m.season, m.week, m.is_playoffs,
       m.away_team_id, m.home_team_id,
       m.away_score, m.home_score, m.margin,
       CASE WHEN m.winner_team_id IS NULL THEN 'T'
            WHEN m.winner_team_id = m.away_team_id THEN 'W' ELSE 'L' END,
       m.playoff_tier
FROM matchups m;

ALTER TABLE matchups DROP COLUMN home_adjustment, DROP COLUMN away_adjustment;

COMMIT;

SELECT grantee, privilege_type FROM information_schema.role_table_grants
WHERE table_name = 'team_games' AND grantee LIKE 'rockwood%' ORDER BY grantee;
