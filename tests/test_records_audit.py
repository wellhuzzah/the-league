r"""
Records audit (2026-11-11): matchups carry ESPN's winner (its tiebreak decides an equal score) and its
final score (league adjustments included); records agrees with matchups; win% follows the NFL (a tie
is half a win and half a loss); the closest-game lists leave out equal scores.

	venv\Scripts\python.exe -B -m unittest tests.test_records_audit -v

The database checks are skipped when .env has no API_DATABASE_URL.
"""

import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

API_URL = dotenv_values(os.path.join(ROOT, ".env")).get("API_DATABASE_URL")
if API_URL:
	os.environ.setdefault("API_DATABASE_URL", API_URL)   # app.database refuses to import without it

from app.routers.matchups import h2h_highlights  # noqa: E402
from app.routers.records import CLOSEST_GAMES, ranked  # noqa: E402
from app.routers.teams import win_pct  # noqa: E402


def fetch(sql, *args):
	import asyncpg

	async def run():
		conn = await asyncpg.connect(API_URL, ssl=False)
		try:
			return await conn.fetch(sql, *args)
		finally:
			await conn.close()
	return asyncio.run(run())


def game(week, hs, as_, winner, home=1, away=2):
	return {"season": 2025, "week": week, "is_playoffs": False, "playoff_tier": "NONE",
	        "home_team_id": home, "away_team_id": away, "home_score": hs, "away_score": as_,
	        "margin": abs(hs - as_), "winner_team_id": winner}


class WinPct(unittest.TestCase):
	def test_nfl_formula(self):
		self.assertEqual(win_pct(6, 6, 1), 0.5)
		self.assertEqual(win_pct(7, 5, 1), round(7.5 / 13, 3))
		self.assertEqual(win_pct(10, 3, 0), round(10 / 13, 3))
		self.assertEqual(win_pct(0, 0, 0), 0.0)


class ClosestGame(unittest.TestCase):
	def test_h2h_closest_leaves_out_equal_scores(self):
		# a tiebreak win at 88-88 is not the closest game; the 1-point game is
		games = [game(1, 88.0, 88.0, 1), game(2, 100.0, 99.0, 1), game(3, 120.0, 90.0, 1)]
		h = h2h_highlights(1, 2, games, {1: "a", 2: "b"})["highlights"]["closest_game"]
		self.assertEqual((h["week"], h["margin"], h["winner"]), (2, 1.0, "a"))

	def test_h2h_closest_none_when_every_game_was_level(self):
		h = h2h_highlights(1, 2, [game(1, 88.0, 88.0, 1)], {1: "a", 2: "b"})
		self.assertIsNone(h["highlights"]["closest_game"])
		self.assertEqual(h["summary"]["team_a_wins"], 1)


@unittest.skipUnless(API_URL, "API_DATABASE_URL not set in .env")
class Data(unittest.TestCase):
	def test_records_match_matchups(self):
		# Every finished team-season: W-L-T and PF/PA in records equal its regular-season games.
		rows = fetch("""
			SELECT r.season, r.team_id FROM records r
			JOIN (SELECT season, team_id,
			             count(*) FILTER (WHERE result = 'W') AS w, count(*) FILTER (WHERE result = 'L') AS l,
			             count(*) FILTER (WHERE result = 'T') AS t, sum(score) AS pf, sum(opp_score) AS pa
			      FROM team_games WHERE NOT is_playoffs GROUP BY 1, 2) g USING (season, team_id)
			WHERE (r.wins, r.losses, r.draws) <> (g.w::int, g.l::int, g.t::int)
			   OR abs(r.points_for - g.pf) > 0.05 OR abs(r.points_against - g.pa) > 0.05""")
		self.assertEqual([dict(r) for r in rows], [])

	def test_most_points_is_the_top_pf(self):
		rows = fetch("""
			SELECT season FROM (SELECT season, most_points, points_for,
			                           max(points_for) OVER (PARTITION BY season) AS top FROM records) x
			GROUP BY season
			HAVING count(*) FILTER (WHERE most_points) <> 1
			    OR bool_or(most_points IS DISTINCT FROM (points_for = top))""")
		self.assertEqual([r["season"] for r in rows], [])

	def test_ties_are_the_eight_espn_ties(self):
		# Equal scores before 2019 were all decided by ESPN's tiebreak; the 8 since are true ties.
		rows = fetch("""SELECT season, count(*) FILTER (WHERE winner_team_id IS NULL) AS ties,
		                       count(*) FILTER (WHERE home_score <> away_score AND winner_team_id IS NULL) AS bad
		                FROM matchups WHERE home_score = away_score OR winner_team_id IS NULL GROUP BY 1""")
		self.assertEqual(sum(r["ties"] for r in rows), 8)
		self.assertEqual(sum(r["bad"] for r in rows), 0)
		self.assertTrue(all(r["season"] >= 2019 for r in rows if r["ties"]))

	def test_adjusted_scores(self):
		# 2011 W9: ryan j 56.00 v mike o 55.00 + 2.00 adjustment = 57.00, mike o's win
		rows = fetch("""SELECT m.home_score, m.away_score, m.away_adjustment, w.owner AS winner
		                FROM matchups m JOIN teams h ON h.team_id = m.home_team_id
		                JOIN teams w ON w.team_id = m.winner_team_id
		                WHERE m.season = 2011 AND m.week = 9 AND h.owner = 'ryan j'""")
		self.assertEqual([tuple(r) for r in rows], [(56, 57, 2, "mike o")])
		n = fetch("SELECT count(*) FROM matchups WHERE home_adjustment <> 0 OR away_adjustment <> 0")[0][0]
		self.assertEqual(n, 3)

	def test_closest_list_leaves_out_equal_scores(self):
		rows = fetch(ranked(CLOSEST_GAMES, "margin", "ASC", "season, week"), 10)
		self.assertGreaterEqual(len(rows), 10)
		self.assertTrue(all(r["margin"] > 0 for r in rows))


if __name__ == "__main__":
	unittest.main()
