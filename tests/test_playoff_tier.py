r"""
Playoff games are winners-bracket games only. matchups.is_playoffs is every game from week 14 on
(consolation ladders included); the H2H playoff counts use matchups.playoff_tier.

	venv\Scripts\python.exe -B -m unittest tests.test_playoff_tier -v

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

from app.routers.matchups import h2h_highlights, is_winners_bracket  # noqa: E402


def game(week, tier, home=1, away=2, hs=100.0, as_=90.0):
	return {"season": 2025, "week": week, "is_playoffs": week >= 14, "playoff_tier": tier,
	        "home_team_id": home, "away_team_id": away, "home_score": hs, "away_score": as_,
	        "margin": abs(hs - as_), "winner_team_id": home if hs > as_ else away if as_ > hs else None}


def fetch(sql, *args):
	import asyncpg

	async def run():
		conn = await asyncpg.connect(API_URL, ssl=False)
		try:
			return await conn.fetch(sql, *args)
		finally:
			await conn.close()
	return asyncio.run(run())


class Highlights(unittest.TestCase):
	def test_playoff_count_is_winners_bracket_only(self):
		games = [game(3, "NONE"), game(14, "WINNERS_BRACKET"), game(15, "WINNERS_CONSOLATION_LADDER"),
		         game(16, "LOSERS_CONSOLATION_LADDER", hs=150.0)]
		h = h2h_highlights(1, 2, games, {1: "a", 2: "b"})
		self.assertEqual(h["summary"]["playoff_games"], 1)
		self.assertEqual(h["summary"]["total_games"], 4)
		# a's best game was the toilet bowl: week 14+, not a playoff game, and the tier says so
		best = h["highlights"]["team_a_best"]
		self.assertEqual((best["week"], best["is_playoffs"], best["playoff_tier"]),
		                 (16, True, "LOSERS_CONSOLATION_LADDER"))

	def test_is_winners_bracket(self):
		self.assertTrue(is_winners_bracket(game(17, "WINNERS_BRACKET")))
		for tier in ("NONE", "WINNERS_CONSOLATION_LADDER", "LOSERS_CONSOLATION_LADDER", None):
			self.assertFalse(is_winners_bracket(game(16, tier)))


@unittest.skipUnless(API_URL, "API_DATABASE_URL not set in .env")
class Data(unittest.TestCase):
	def test_tier_matches_week(self):
		# Every game has a tier, and it is NONE exactly in the regular season.
		rows = fetch("""SELECT count(*) FILTER (WHERE playoff_tier IS NULL) AS missing,
		                       count(*) FILTER (WHERE (playoff_tier = 'NONE') = is_playoffs) AS mismatched
		                FROM matchups""")
		self.assertEqual((rows[0]["missing"], rows[0]["mismatched"]), (0, 0))

	def test_one_champion_game_per_finished_season(self):
		# The winners bracket ends in one final per season, played by that season's champion.
		rows = fetch("""SELECT r.season FROM records r WHERE r.championship AND NOT EXISTS (
		                    SELECT 1 FROM matchups m WHERE m.season = r.season
		                      AND m.playoff_tier = 'WINNERS_BRACKET'
		                      AND r.team_id IN (m.home_team_id, m.away_team_id)
		                      AND m.week = (SELECT max(week) FROM matchups x WHERE x.season = r.season))""")
		self.assertEqual([r["season"] for r in rows], [])


if __name__ == "__main__":
	unittest.main()
