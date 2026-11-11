r"""
Streak tests: STREAKS from app/routers/records.py, run on made-up games against the database as
rockwood_api (read-only), plus a live check. A streak's start and end are its first and last games
in (season, week) order; a tie ends a streak.

	venv\Scripts\python.exe -B -m unittest tests.test_streaks -v

Skipped when .env has no API_DATABASE_URL.
"""

import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

from app.routers.records import STREAKS  # noqa: E402

API_URL = dotenv_values(os.path.join(ROOT, ".env")).get("API_DATABASE_URL")

SOURCE = """FROM team_games tg
        JOIN teams t ON t.team_id = tg.team_id"""


def fetch(sql, *args):
	import asyncpg

	async def run():
		conn = await asyncpg.connect(API_URL, ssl=False)
		try:
			return await conn.fetch(sql, *args)
		finally:
			await conn.close()
	return asyncio.run(run())


def streaks(games):
	"""Every streak (won, owner, length, start, end) for made-up (team_id, season, week, result) rows."""
	assert SOURCE in STREAKS
	values = ", ".join(f"({t}, {s}, {w}, '{r}')" for t, s, w, r in games)
	sql = STREAKS.replace(SOURCE, f"""FROM (VALUES {values}) AS tg(team_id, season, week, result)
		JOIN (VALUES (1, 'a'), (2, 'b')) AS t(team_id, owner) ON t.team_id = tg.team_id""")
	return {(r["won"], r["owner"], r["streak_length"],
	         (r["start_season"], r["start_week"]), (r["end_season"], r["end_week"]))
	        for r in fetch(sql, 1000)}


@unittest.skipUnless(API_URL, "API_DATABASE_URL not set in .env")
class Streaks(unittest.TestCase):
	def test_streak_across_seasons(self):
		# Team a wins 2020 weeks 15-16 and 2021 weeks 1-3: first game 2020 W15, last 2021 W3
		# (the old MIN/MAX gave W1 and W16).
		got = streaks([(1, 2020, 14, 'L'), (1, 2020, 15, 'W'), (1, 2020, 16, 'W'),
		               (1, 2021, 1, 'W'), (1, 2021, 2, 'W'), (1, 2021, 3, 'W'), (1, 2021, 4, 'L')])
		self.assertIn((1, "a", 5, (2020, 15), (2021, 3)), got)
		self.assertIn((0, "a", 1, (2020, 14), (2020, 14)), got)
		self.assertIn((0, "a", 1, (2021, 4), (2021, 4)), got)

	def test_streak_across_three_seasons(self):
		got = streaks([(2, 2009, 15, 'L'), (2, 2010, 1, 'L'), (2, 2010, 16, 'L'), (2, 2011, 8, 'L')])
		self.assertEqual(got, {(0, "b", 4, (2009, 15), (2011, 8))})

	def test_tie_ends_a_streak(self):
		got = streaks([(1, 2020, 1, 'W'), (1, 2020, 2, 'W'), (1, 2020, 3, 'T'), (1, 2020, 4, 'W')])
		self.assertEqual(got, {(1, "a", 2, (2020, 1), (2020, 2)), (1, "a", 1, (2020, 4), (2020, 4))})

	def test_live_streaks_run_forward(self):
		# Every live streak ends at or after its start, and one in a single season spans at least
		# length weeks (more when a playoff bye falls inside it; byes aren't stored).
		rows = fetch(STREAKS, 1000)
		self.assertGreater(len(rows), 1000)
		for r in rows:
			start, end = (r["start_season"], r["start_week"]), (r["end_season"], r["end_week"])
			self.assertLessEqual(start, end, dict(r))
			if r["start_season"] == r["end_season"]:
				self.assertGreaterEqual(r["end_week"] - r["start_week"] + 1, r["streak_length"], dict(r))


if __name__ == "__main__":
	unittest.main()
