r"""
Luck (Bullshit Index) tests: LUCK_WEEKS from app/routers/seasons.py, run against the database as
rockwood_api. Read-only. The definition: per team-week, actual result (W 1, T 0.5, L 0) minus the
share of that week's pool the team outscored (an equal score counts half); the pool is every
regular-season game and, in the playoffs, winners-bracket games only; a game where either side
scored 0 drops both teams.

	venv\Scripts\python.exe -B -m unittest tests.test_luck -v

Skipped when .env has no API_DATABASE_URL.
"""

import asyncio
import os
import sys
import unittest
from fractions import Fraction as F

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

from app.routers.seasons import LUCK_WEEKS  # noqa: E402

API_URL = dotenv_values(os.path.join(ROOT, ".env")).get("API_DATABASE_URL")


def fetch(sql, *args):
	import asyncpg

	async def run():
		conn = await asyncpg.connect(API_URL, ssl=False)
		try:
			return await conn.fetch(sql, *args)
		finally:
			await conn.close()
	return asyncio.run(run())


def week(season, wk):
	"""owner -> (actual, expected, luck) for one week's pool."""
	rows = fetch(LUCK_WEEKS.format(where="AND tg.season = $1 AND tg.week = $2") + """
		SELECT t.owner, w.actual, w.expected, w.actual - w.expected AS luck
		FROM weekly w JOIN teams t ON t.team_id = w.team_id""", season, wk)
	return {r["owner"]: (r["actual"], r["expected"], r["luck"]) for r in rows}


def close(a, b):
	return abs(float(a) - float(b)) < 1e-9


@unittest.skipUnless(API_URL, "API_DATABASE_URL not set in .env")
class Luck(unittest.TestCase):
	def test_every_week_sums_to_zero(self):
		rows = fetch(LUCK_WEEKS.format(where="") + """
			SELECT season, week, SUM(actual - expected) AS total, count(*) AS teams
			FROM weekly GROUP BY season, week""")
		self.assertGreaterEqual(len(rows), 285)    # 2009-2025 complete, 2026 through week 8
		bad = [(int(r["season"]), r["week"], r["total"]) for r in rows if not close(r["total"], 0)]
		self.assertEqual(bad, [])

	def test_hand_checked_week(self):
		# 2009 week 1: 12 teams. Three at 88.00: joey r and mike o tied each other, greg d lost.
		w = week(2009, 1)
		self.assertEqual(len(w), 12)
		expect = {    # owner: (actual, outscored, equal others)
			"robert h": (1, 11, 0), "joey j": (1, 10, 0), "kevin m": (0, 6, 0),
			"joey r": (F(1, 2), 3, 2), "mike o": (F(1, 2), 3, 2), "greg d": (0, 3, 2),
			"dan t": (0, 1, 0), "ben m": (0, 0, 0)}
		for owner, (actual, outscored, equal) in expect.items():
			with self.subTest(owner=owner):
				exp = (outscored + F(equal, 2)) / 11
				self.assertTrue(close(w[owner][0], actual))
				self.assertTrue(close(w[owner][1], exp), (w[owner][1], float(exp)))
				self.assertTrue(close(w[owner][2], actual - exp))
		self.assertTrue(close(w["joey r"][2], F(3, 22)))     # 0.5 - 4/11
		self.assertTrue(close(w["kevin m"][2], F(-6, 11)))
		self.assertTrue(close(w["robert h"][2], 0))           # top score and a win: no luck

	def test_ties_count_half(self):
		w = week(2009, 1)
		self.assertTrue(close(w["joey r"][0], 0.5) and close(w["mike o"][0], 0.5))
		# a tied playoff final (2024 week 17, 110.50 each): half a win each, expected half: no luck
		final = week(2024, 17)
		self.assertEqual(sorted(final), ["brian h", "joey r"])
		for owner in final:
			self.assertTrue(close(final[owner][0], 0.5) and close(final[owner][1], 0.5) and close(final[owner][2], 0))

	def test_bye_week(self):
		# 2021 week 14: 9-team playoff, one winners-bracket game (ryan j v kenny d) and 7 byes. Teams
		# on a bye have no game, so no luck and no place in the pool.
		w = week(2021, 14)
		self.assertEqual(sorted(w), ["kenny d", "ryan j"])
		for owner in ("owens", "jake d", "ryan l", "joey j", "ben m", "josh d", "brian h"):
			self.assertNotIn(owner, w)
		for actual, expected, luck in w.values():
			self.assertTrue(close(expected, actual) and close(luck, 0))    # a pool of 2: winner outscored the loser

	def test_consolation_games_excluded(self):
		# 2021 week 16: winners bracket owens v jake d and ben m v josh d; the 3rd-place-ladder games
		# (brian h v ryan l, joey j v ryan j) and the losers ladder count for nothing.
		w = week(2021, 16)
		self.assertEqual(sorted(w), ["ben m", "jake d", "josh d", "owens"])
		# 2016 week 16: mike o v dan t tied 102.50 in a consolation game; only the final counts
		self.assertEqual(sorted(week(2016, 16)), ["greg d", "jake d"])
		# 2017 week 16: ben m's 0.00 came in a losers-ladder game
		self.assertNotIn("ben m", week(2017, 16))

	def test_zero_score_game_drops_both_teams(self):
		# No counted game has a 0 yet, so run the same SQL on a made-up week: A 100 v B 0, C 90 v D 80.
		source = "FROM team_games tg"
		sql = LUCK_WEEKS.format(where="")
		self.assertIn(source, sql)
		sql = sql.replace(source, """FROM (VALUES
			(1, 1, 1, 100.0, 0.0, 'W', false, 'NONE'), (1, 1, 2, 0.0, 100.0, 'L', false, 'NONE'),
			(1, 1, 3, 90.0, 80.0, 'W', false, 'NONE'), (1, 1, 4, 80.0, 90.0, 'L', false, 'NONE')
		) AS tg(season, week, team_id, score, opp_score, result, is_playoffs, playoff_tier)""")
		rows = {r["team_id"]: r for r in fetch(sql + "SELECT team_id, actual, expected FROM weekly")}
		self.assertEqual(sorted(rows), [3, 4])                       # A and B are both dropped
		self.assertTrue(close(rows[3]["actual"] - rows[3]["expected"], 0))
		self.assertTrue(close(rows[4]["actual"] - rows[4]["expected"], 0))


if __name__ == "__main__":
	unittest.main()
