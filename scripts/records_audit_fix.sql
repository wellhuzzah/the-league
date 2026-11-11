-- Records audit fix (2026-11-11; findings in docs/records_audit.md). Run as postgres; one transaction,
-- and every UPDATE checks the row's current values first, so a row that isn't as expected aborts it all:
--   psql -h localhost -U postgres -d rockwood -X -f scripts\records_audit_fix.sql
-- Back up first: pg_dump -h localhost -U postgres -d rockwood -t matchups -t records -Fc -f backups\matchups_records_20261111.dump
-- Undo: scripts\rollback_records_audit.sql.
--
-- 1. schema_v3.sql section 8: matchups.home_adjustment / away_adjustment, and team_games gains them.
-- 2. matchups, 19 rows, all to ESPN's archived schedule (newest mMatchup+mMatchupScore per season):
--    - 16 equal-score games get ESPN's tiebreak winner; scores unchanged.
--    - 3 games get ESPN's score adjustment added to the stored score (ESPN's totalPoints leaves it
--      out); 2011 W9 also changes winner (ryan j 56 v mike o 55+2 = 57).
-- 3. records, 31 rows, to ESPN's mTeam record.overall: W-L of 2010 josh s and 2016 joey j; PF/PA of
--    2014 brian h and every 2024-25 row (they held weeks 1-12 only); most_points for 2025 moves from
--    greg d to ben m.
\set ON_ERROR_STOP on
BEGIN;

ALTER TABLE matchups ADD COLUMN home_adjustment numeric(6,2) NOT NULL DEFAULT 0,
                     ADD COLUMN away_adjustment numeric(6,2) NOT NULL DEFAULT 0;

CREATE OR REPLACE VIEW team_games AS
SELECT m.id AS matchup_id, m.season, m.week, m.is_playoffs,
       m.home_team_id AS team_id, m.away_team_id AS opp_id,
       m.home_score AS score, m.away_score AS opp_score, m.margin,
       CASE WHEN m.winner_team_id IS NULL THEN 'T'
            WHEN m.winner_team_id = m.home_team_id THEN 'W' ELSE 'L' END AS result,
       m.playoff_tier,
       m.home_adjustment AS adjustment, m.away_adjustment AS opp_adjustment
FROM matchups m
UNION ALL
SELECT m.id, m.season, m.week, m.is_playoffs,
       m.away_team_id, m.home_team_id,
       m.away_score, m.home_score, m.margin,
       CASE WHEN m.winner_team_id IS NULL THEN 'T'
            WHEN m.winner_team_id = m.away_team_id THEN 'W' ELSE 'L' END,
       m.playoff_tier,
       m.away_adjustment, m.home_adjustment
FROM matchups m;

DO $$
BEGIN
  -- 2009 W1 joey r v mike o: 88.00-88.00 winner tie -> 88.00-88.00 winner mike o
  UPDATE matchups SET home_score = 88.00, away_score = 88.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 11
   WHERE id = 1350 AND home_score = 88.00 AND away_score = 88.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1350 (2009 W1 joey r v mike o) is not as expected'; END IF;
  -- 2009 W10 nick o v josh d: 91.00-91.00 winner tie -> 91.00-91.00 winner nick o
  UPDATE matchups SET home_score = 91.00, away_score = 91.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 3
   WHERE id = 1400 AND home_score = 91.00 AND away_score = 91.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1400 (2009 W10 nick o v josh d) is not as expected'; END IF;
  -- 2009 W12 dan t v joey r: 99.00-99.00 winner tie -> 99.00-99.00 winner dan t
  UPDATE matchups SET home_score = 99.00, away_score = 99.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 12
   WHERE id = 1416 AND home_score = 99.00 AND away_score = 99.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1416 (2009 W12 dan t v joey r) is not as expected'; END IF;
  -- 2010 W1 ryan l v dan t: 84.00-80.00 winner ryan l -> 84.00-81.00 winner ryan l (adjustments 0.00/1.00)
  UPDATE matchups SET home_score = 84.00, away_score = 81.00, home_adjustment = 0.00, away_adjustment = 1.00, winner_team_id = 8
   WHERE id = 1443 AND home_score = 84.00 AND away_score = 80.00 AND winner_team_id IS NOT DISTINCT FROM 8;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1443 (2010 W1 ryan l v dan t) is not as expected'; END IF;
  -- 2011 W8 dan t v ryan j: 101.00-101.00 winner tie -> 101.00-101.00 winner ryan j
  UPDATE matchups SET home_score = 101.00, away_score = 101.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 13
   WHERE id = 1589 AND home_score = 101.00 AND away_score = 101.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1589 (2011 W8 dan t v ryan j) is not as expected'; END IF;
  -- 2011 W9 ryan j v mike o: 56.00-55.00 winner ryan j -> 56.00-57.00 winner mike o (adjustments 0.00/2.00)
  UPDATE matchups SET home_score = 56.00, away_score = 57.00, home_adjustment = 0.00, away_adjustment = 2.00, winner_team_id = 11
   WHERE id = 1595 AND home_score = 56.00 AND away_score = 55.00 AND winner_team_id IS NOT DISTINCT FROM 13;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1595 (2011 W9 ryan j v mike o) is not as expected'; END IF;
  -- 2012 W2 joey j v josh d: 80.00-69.00 winner joey j -> 86.00-69.00 winner joey j (adjustments 6.00/0.00)
  UPDATE matchups SET home_score = 86.00, away_score = 69.00, home_adjustment = 6.00, away_adjustment = 0.00, winner_team_id = 2
   WHERE id = 1654 AND home_score = 80.00 AND away_score = 69.00 AND winner_team_id IS NOT DISTINCT FROM 2;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1654 (2012 W2 joey j v josh d) is not as expected'; END IF;
  -- 2012 W6 josh d v greg d: 87.00-87.00 winner tie -> 87.00-87.00 winner greg d
  UPDATE matchups SET home_score = 87.00, away_score = 87.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 7
   WHERE id = 1688 AND home_score = 87.00 AND away_score = 87.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1688 (2012 W6 josh d v greg d) is not as expected'; END IF;
  -- 2012 W12 greg d v jake d: 73.00-73.00 winner tie -> 73.00-73.00 winner greg d
  UPDATE matchups SET home_score = 73.00, away_score = 73.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 7
   WHERE id = 1726 AND home_score = 73.00 AND away_score = 73.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1726 (2012 W12 greg d v jake d) is not as expected'; END IF;
  -- 2013 W7 dan t v nick o: 90.00-90.00 winner tie -> 90.00-90.00 winner nick o
  UPDATE matchups SET home_score = 90.00, away_score = 90.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 3
   WHERE id = 1803 AND home_score = 90.00 AND away_score = 90.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1803 (2013 W7 dan t v nick o) is not as expected'; END IF;
  -- 2013 W9 kenny d v greg d: 84.00-84.00 winner tie -> 84.00-84.00 winner kenny d
  UPDATE matchups SET home_score = 84.00, away_score = 84.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 15
   WHERE id = 1817 AND home_score = 84.00 AND away_score = 84.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1817 (2013 W9 kenny d v greg d) is not as expected'; END IF;
  -- 2014 W3 josh d v joey r: 87.00-87.00 winner tie -> 87.00-87.00 winner josh d
  UPDATE matchups SET home_score = 87.00, away_score = 87.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 4
   WHERE id = 1888 AND home_score = 87.00 AND away_score = 87.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1888 (2014 W3 josh d v joey r) is not as expected'; END IF;
  -- 2014 W13 ryan l v ben m: 87.00-87.00 winner tie -> 87.00-87.00 winner ben m
  UPDATE matchups SET home_score = 87.00, away_score = 87.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 5
   WHERE id = 1958 AND home_score = 87.00 AND away_score = 87.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 1958 (2014 W13 ryan l v ben m) is not as expected'; END IF;
  -- 2015 W6 ryan l v kenny d: 97.00-97.00 winner tie -> 97.00-97.00 winner ryan l
  UPDATE matchups SET home_score = 97.00, away_score = 97.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 8
   WHERE id = 2021 AND home_score = 97.00 AND away_score = 97.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2021 (2015 W6 ryan l v kenny d) is not as expected'; END IF;
  -- 2016 W4 brian h v mike o: 94.50-94.50 winner tie -> 94.50-94.50 winner mike o
  UPDATE matchups SET home_score = 94.50, away_score = 94.50, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 11
   WHERE id = 2121 AND home_score = 94.50 AND away_score = 94.50 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2121 (2016 W4 brian h v mike o) is not as expected'; END IF;
  -- 2016 W16 mike o v dan t: 102.50-102.50 winner tie -> 102.50-102.50 winner mike o
  UPDATE matchups SET home_score = 102.50, away_score = 102.50, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 11
   WHERE id = 2202 AND home_score = 102.50 AND away_score = 102.50 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2202 (2016 W16 mike o v dan t) is not as expected'; END IF;
  -- 2018 W3 brian h v owens: 122.00-122.00 winner tie -> 122.00-122.00 winner owens
  UPDATE matchups SET home_score = 122.00, away_score = 122.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 6
   WHERE id = 2334 AND home_score = 122.00 AND away_score = 122.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2334 (2018 W3 brian h v owens) is not as expected'; END IF;
  -- 2018 W3 ryan j v greg d: 113.00-113.00 winner tie -> 113.00-113.00 winner ryan j
  UPDATE matchups SET home_score = 113.00, away_score = 113.00, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 13
   WHERE id = 2338 AND home_score = 113.00 AND away_score = 113.00 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 2338 (2018 W3 ryan j v greg d) is not as expected'; END IF;
  -- 2024 W17 brian h v joey r: 110.50-110.50 winner tie -> 110.50-110.50 winner brian h
  UPDATE matchups SET home_score = 110.50, away_score = 110.50, home_adjustment = 0.00, away_adjustment = 0.00, winner_team_id = 14
   WHERE id = 3097 AND home_score = 110.50 AND away_score = 110.50 AND winner_team_id IS NOT DISTINCT FROM NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'matchup 3097 (2024 W17 brian h v joey r) is not as expected'; END IF;

  -- 2010 josh s: (4, 12, 0, Decimal('995.0'), Decimal('1169.0'), False) -> (2, 11, 0, Decimal('995.00'), Decimal('1169.00'), False)
  UPDATE records SET wins = 2, losses = 11 WHERE id = 24 AND wins = 4 AND losses = 12 AND draws = 0 AND points_for = 995.0 AND points_against = 1169.0 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 24 (2010 josh s) is not as expected'; END IF;
  -- 2014 brian h: (9, 4, 0, Decimal('1176.0'), Decimal('1139.0'), False) -> (9, 4, 0, Decimal('1237.00'), Decimal('1111.00'), False)
  UPDATE records SET points_for = 1237.00, points_against = 1111.00 WHERE id = 79 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1176.0 AND points_against = 1139.0 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 79 (2014 brian h) is not as expected'; END IF;
  -- 2016 joey j: (8, 5, 0, Decimal('1232.0'), Decimal('1367.5'), False) -> (5, 8, 0, Decimal('1232.00'), Decimal('1367.50'), False)
  UPDATE records SET wins = 5, losses = 8 WHERE id = 96 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1232.0 AND points_against = 1367.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 96 (2016 joey j) is not as expected'; END IF;
  -- 2024 jake d: (6, 7, 0, Decimal('1225.5'), Decimal('1209.0'), False) -> (6, 7, 0, Decimal('1359.00'), Decimal('1345.50'), False)
  UPDATE records SET points_for = 1359.00, points_against = 1345.50 WHERE id = 232 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1225.5 AND points_against = 1209.0 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 232 (2024 jake d) is not as expected'; END IF;
  -- 2024 joey j: (7, 6, 0, Decimal('1219.5'), Decimal('1242.5'), False) -> (7, 6, 0, Decimal('1328.00'), Decimal('1332.00'), False)
  UPDATE records SET points_for = 1328.00, points_against = 1332.00 WHERE id = 229 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1219.5 AND points_against = 1242.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 229 (2024 joey j) is not as expected'; END IF;
  -- 2024 nick o: (6, 6, 1, Decimal('1279.5'), Decimal('1223.5'), False) -> (6, 6, 1, Decimal('1361.50'), Decimal('1309.00'), False)
  UPDATE records SET points_for = 1361.50, points_against = 1309.00 WHERE id = 224 AND wins = 6 AND losses = 6 AND draws = 1 AND points_for = 1279.5 AND points_against = 1223.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 224 (2024 nick o) is not as expected'; END IF;
  -- 2024 josh d: (7, 6, 0, Decimal('1229.5'), Decimal('1180.5'), False) -> (7, 6, 0, Decimal('1366.00'), Decimal('1314.00'), False)
  UPDATE records SET points_for = 1366.00, points_against = 1314.00 WHERE id = 226 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1229.5 AND points_against = 1180.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 226 (2024 josh d) is not as expected'; END IF;
  -- 2024 ben m: (5, 8, 0, Decimal('1184.5'), Decimal('1269.5'), False) -> (5, 8, 0, Decimal('1319.00'), Decimal('1364.50'), False)
  UPDATE records SET points_for = 1319.00, points_against = 1364.50 WHERE id = 233 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1184.5 AND points_against = 1269.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 233 (2024 ben m) is not as expected'; END IF;
  -- 2024 owens: (6, 6, 1, Decimal('1183.0'), Decimal('1201.5'), False) -> (6, 6, 1, Decimal('1269.00'), Decimal('1354.00'), False)
  UPDATE records SET points_for = 1269.00, points_against = 1354.00 WHERE id = 228 AND wins = 6 AND losses = 6 AND draws = 1 AND points_for = 1183.0 AND points_against = 1201.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 228 (2024 owens) is not as expected'; END IF;
  -- 2024 greg d: (8, 5, 0, Decimal('1180.5'), Decimal('1234.0'), False) -> (8, 5, 0, Decimal('1287.00'), Decimal('1293.00'), False)
  UPDATE records SET points_for = 1287.00, points_against = 1293.00 WHERE id = 223 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1180.5 AND points_against = 1234.0 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 223 (2024 greg d) is not as expected'; END IF;
  -- 2024 ryan l: (4, 9, 0, Decimal('1095.0'), Decimal('1283.5'), False) -> (4, 9, 0, Decimal('1190.00'), Decimal('1418.00'), False)
  UPDATE records SET points_for = 1190.00, points_against = 1418.00 WHERE id = 230 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1095.0 AND points_against = 1283.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 230 (2024 ryan l) is not as expected'; END IF;
  -- 2024 kevin m: (9, 4, 0, Decimal('1282.0'), Decimal('1156.5'), False) -> (9, 4, 0, Decimal('1403.50'), Decimal('1249.00'), False)
  UPDATE records SET points_for = 1403.50, points_against = 1249.00 WHERE id = 225 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1282.0 AND points_against = 1156.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 225 (2024 kevin m) is not as expected'; END IF;
  -- 2024 joey r: (8, 5, 0, Decimal('1326.0'), Decimal('1108.5'), False) -> (8, 5, 0, Decimal('1415.50'), Decimal('1217.00'), False)
  UPDATE records SET points_for = 1415.50, points_against = 1217.00 WHERE id = 222 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1326.0 AND points_against = 1108.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 222 (2024 joey r) is not as expected'; END IF;
  -- 2024 mike o: (7, 6, 0, Decimal('1333.0'), Decimal('1339.0'), True) -> (7, 6, 0, Decimal('1418.50'), Decimal('1421.00'), True)
  UPDATE records SET points_for = 1418.50, points_against = 1421.00 WHERE id = 227 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1333.0 AND points_against = 1339.0 AND most_points = true;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 227 (2024 mike o) is not as expected'; END IF;
  -- 2024 dan t: (5, 8, 0, Decimal('1115.0'), Decimal('1042.5'), False) -> (5, 8, 0, Decimal('1174.00'), Decimal('1149.00'), False)
  UPDATE records SET points_for = 1174.00, points_against = 1149.00 WHERE id = 234 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1115.0 AND points_against = 1042.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 234 (2024 dan t) is not as expected'; END IF;
  -- 2024 ryan j: (4, 9, 0, Decimal('1193.0'), Decimal('1309.5'), False) -> (4, 9, 0, Decimal('1285.50'), Decimal('1431.00'), False)
  UPDATE records SET points_for = 1285.50, points_against = 1431.00 WHERE id = 231 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1193.0 AND points_against = 1309.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 231 (2024 ryan j) is not as expected'; END IF;
  -- 2024 brian h: (8, 5, 0, Decimal('1260.5'), Decimal('1306.5'), False) -> (8, 5, 0, Decimal('1413.00'), Decimal('1392.50'), False)
  UPDATE records SET points_for = 1413.00, points_against = 1392.50 WHERE id = 221 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1260.5 AND points_against = 1306.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 221 (2024 brian h) is not as expected'; END IF;
  -- 2025 jake d: (4, 9, 0, Decimal('1164.9'), Decimal('1308.2'), False) -> (4, 9, 0, Decimal('1240.10'), Decimal('1385.60'), False)
  UPDATE records SET points_for = 1240.10, points_against = 1385.60 WHERE id = 220 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1164.9 AND points_against = 1308.2 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 220 (2025 jake d) is not as expected'; END IF;
  -- 2025 joey j: (4, 9, 0, Decimal('1252.6'), Decimal('1285.9'), False) -> (4, 9, 0, Decimal('1344.70'), Decimal('1390.90'), False)
  UPDATE records SET points_for = 1344.70, points_against = 1390.90 WHERE id = 218 AND wins = 4 AND losses = 9 AND draws = 0 AND points_for = 1252.6 AND points_against = 1285.9 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 218 (2025 joey j) is not as expected'; END IF;
  -- 2025 nick o: (6, 7, 0, Decimal('1274.3'), Decimal('1369.0'), False) -> (6, 7, 0, Decimal('1398.30'), Decimal('1458.20'), False)
  UPDATE records SET points_for = 1398.30, points_against = 1458.20 WHERE id = 217 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1274.3 AND points_against = 1369.0 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 217 (2025 nick o) is not as expected'; END IF;
  -- 2025 josh d: (9, 4, 0, Decimal('1234.3'), Decimal('1158.6'), False) -> (9, 4, 0, Decimal('1311.70'), Decimal('1233.80'), False)
  UPDATE records SET points_for = 1311.70, points_against = 1233.80 WHERE id = 208 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1234.3 AND points_against = 1158.6 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 208 (2025 josh d) is not as expected'; END IF;
  -- 2025 ben m: (6, 7, 0, Decimal('1462.4'), Decimal('1339.3'), False) -> (6, 7, 0, Decimal('1561.50'), Decimal('1456.00'), True)
  UPDATE records SET points_for = 1561.50, points_against = 1456.00, most_points = true WHERE id = 219 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1462.4 AND points_against = 1339.3 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 219 (2025 ben m) is not as expected'; END IF;
  -- 2025 owens: (7, 6, 0, Decimal('1142.5'), Decimal('1311.5'), False) -> (7, 6, 0, Decimal('1211.90'), Decimal('1426.00'), False)
  UPDATE records SET points_for = 1211.90, points_against = 1426.00 WHERE id = 214 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1142.5 AND points_against = 1311.5 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 214 (2025 owens) is not as expected'; END IF;
  -- 2025 greg d: (8, 5, 0, Decimal('1475.3'), Decimal('1258.4'), True) -> (8, 5, 0, Decimal('1554.00'), Decimal('1381.00'), False)
  UPDATE records SET points_for = 1554.00, points_against = 1381.00, most_points = false WHERE id = 211 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1475.3 AND points_against = 1258.4 AND most_points = true;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 211 (2025 greg d) is not as expected'; END IF;
  -- 2025 ryan l: (6, 7, 0, Decimal('1190.1'), Decimal('1243.6'), False) -> (6, 7, 0, Decimal('1306.80'), Decimal('1342.70'), False)
  UPDATE records SET points_for = 1306.80, points_against = 1342.70 WHERE id = 210 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1190.1 AND points_against = 1243.6 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 210 (2025 ryan l) is not as expected'; END IF;
  -- 2025 kevin m: (6, 7, 0, Decimal('1298.9'), Decimal('1236.9'), False) -> (6, 7, 0, Decimal('1399.80'), Decimal('1324.10'), False)
  UPDATE records SET points_for = 1399.80, points_against = 1324.10 WHERE id = 212 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1298.9 AND points_against = 1236.9 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 212 (2025 kevin m) is not as expected'; END IF;
  -- 2025 joey r: (6, 7, 0, Decimal('1138.5'), Decimal('1254.4'), False) -> (6, 7, 0, Decimal('1243.50'), Decimal('1346.50'), False)
  UPDATE records SET points_for = 1243.50, points_against = 1346.50 WHERE id = 213 AND wins = 6 AND losses = 7 AND draws = 0 AND points_for = 1138.5 AND points_against = 1254.4 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 213 (2025 joey r) is not as expected'; END IF;
  -- 2025 mike o: (8, 5, 0, Decimal('1332.7'), Decimal('1253.6'), False) -> (8, 5, 0, Decimal('1421.90'), Decimal('1377.60'), False)
  UPDATE records SET points_for = 1421.90, points_against = 1377.60 WHERE id = 209 AND wins = 8 AND losses = 5 AND draws = 0 AND points_for = 1332.7 AND points_against = 1253.6 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 209 (2025 mike o) is not as expected'; END IF;
  -- 2025 dan t: (7, 6, 0, Decimal('1161.1'), Decimal('1142.1'), False) -> (7, 6, 0, Decimal('1283.70'), Decimal('1220.80'), False)
  UPDATE records SET points_for = 1283.70, points_against = 1220.80 WHERE id = 215 AND wins = 7 AND losses = 6 AND draws = 0 AND points_for = 1161.1 AND points_against = 1142.1 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 215 (2025 dan t) is not as expected'; END IF;
  -- 2025 ryan j: (5, 8, 0, Decimal('1177.2'), Decimal('1307.9'), False) -> (5, 8, 0, Decimal('1264.40'), Decimal('1408.80'), False)
  UPDATE records SET points_for = 1264.40, points_against = 1408.80 WHERE id = 216 AND wins = 5 AND losses = 8 AND draws = 0 AND points_for = 1177.2 AND points_against = 1307.9 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 216 (2025 ryan j) is not as expected'; END IF;
  -- 2025 brian h: (9, 4, 0, Decimal('1386.3'), Decimal('1221.7'), False) -> (9, 4, 0, Decimal('1500.80'), Decimal('1291.10'), False)
  UPDATE records SET points_for = 1500.80, points_against = 1291.10 WHERE id = 207 AND wins = 9 AND losses = 4 AND draws = 0 AND points_for = 1386.3 AND points_against = 1221.7 AND most_points = false;
  IF NOT FOUND THEN RAISE EXCEPTION 'records 207 (2025 brian h) is not as expected'; END IF;
END $$;

COMMIT;

-- Checks (read only): no equal-score game ESPN decided is left a tie, and regular-season W-L from
-- matchups matches records for every finished team-season.
SELECT count(*) AS equal_score_ties FROM matchups WHERE home_score = away_score AND winner_team_id IS NULL;
SELECT r.season, t.owner, r.wins, r.losses, r.draws, g.w, g.l, g.t
FROM records r JOIN teams t USING (team_id)
JOIN (SELECT season, team_id, count(*) FILTER (WHERE result = 'W') w, count(*) FILTER (WHERE result = 'L') l,
             count(*) FILTER (WHERE result = 'T') t
      FROM team_games WHERE NOT is_playoffs GROUP BY 1, 2) g USING (season, team_id)
WHERE (r.wins, r.losses, r.draws) <> (g.w::int, g.l::int, g.t::int);
