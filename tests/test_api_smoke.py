r"""
API smoke test: every GET route in app.routes, called with realistic parameters, must return 200 and
the top-level shape recorded in SHAPES (a list's length floor and its first item's keys, or a
dict's keys). A GET route with no case here fails the test, so new routes get covered.

Unlike test_guardrails.py this needs the database: it starts uvicorn from this working tree on a
spare loopback port (so it tests the code on disk, not the running service), which reads .env and
connects as rockwood_api, exactly as production does. Read-only: it never calls POST /tower/score.

	venv\Scripts\python.exe -B -m unittest tests.test_api_smoke -v

Skipped when .env has no API_DATABASE_URL. When an endpoint's response shape changes on purpose,
update its SHAPES entry in the same change.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

API_URL = dotenv_values(os.path.join(ROOT, ".env")).get("API_DATABASE_URL")

# (route template as in app.routes, URL to call). Years: 2025 is the last complete season,
# 2026 is in progress. Team 1 and 2, week 5, Josh Allen and WR all have data in every table.
CASES = [
	("/", "/"),
	("/seasons/", "/seasons/"),
	("/seasons/{year}/standings", "/seasons/2025/standings"),
	("/seasons/{year}/standings", "/seasons/2026/standings"),
	("/seasons/{year}/weekly-scores", "/seasons/2025/weekly-scores"),
	("/seasons/{year}/luck", "/seasons/2025/luck"),
	("/seasons/{year}/top-scorer", "/seasons/2025/top-scorer"),
	("/records/alltime", "/records/alltime"),
	("/records/scoring/highest-season", "/records/scoring/highest-season"),
	("/records/scoring/lowest-season", "/records/scoring/lowest-season"),
	("/records/scoring/best-single-week", "/records/scoring/best-single-week"),
	("/records/scoring/worst-single-week", "/records/scoring/worst-single-week"),
	("/records/streaks/wins", "/records/streaks/wins"),
	("/records/streaks/losses", "/records/streaks/losses"),
	("/records/draft/most-drafted-position", "/records/draft/most-drafted-position"),
	("/records/draft/by-owner", "/records/draft/by-owner"),
	("/records/scoring/highest-losing-scores", "/records/scoring/highest-losing-scores"),
	("/records/scoring/lowest-winning-scores", "/records/scoring/lowest-winning-scores"),
	("/draft/", "/draft/"),
	("/draft/tendencies", "/draft/tendencies"),
	("/draft/player/{player_name:path}", "/draft/player/Josh%20Allen"),
	("/draft/value/history", "/draft/value/history"),
	("/draft/{year}", "/draft/2025"),
	("/draft/{year}/by-team", "/draft/2025/by-team"),
	("/draft/{year}/keepers", "/draft/2025/keepers"),
	("/draft/{year}/value", "/draft/2025/value"),
	("/matchups/h2h/{team_id_a}/{team_id_b}", "/matchups/h2h/1/2"),
	("/matchups/season/{year}", "/matchups/season/2025"),
	("/matchups/season/{year}/week/{week}", "/matchups/season/2025/week/5"),
	("/matchups/records/highest-scores", "/matchups/records/highest-scores"),
	("/matchups/records/biggest-blowouts", "/matchups/records/biggest-blowouts"),
	("/matchups/records/closest-games", "/matchups/records/closest-games"),
	("/matchups/random-h2h", "/matchups/random-h2h"),
	("/teams/", "/teams/"),
	("/teams/{team_id}", "/teams/1"),
	("/boxscores/top-performances", "/boxscores/top-performances"),
	("/boxscores/top-performances", "/boxscores/top-performances?position=QB"),
	("/boxscores/position-breakdown", "/boxscores/position-breakdown"),
	("/boxscores/team/{team_id}", "/boxscores/team/1?season=2025"),
	("/boxscores/team/{team_id}/best-weeks", "/boxscores/team/1/best-weeks"),
	("/boxscores/team/{team_id}/position-totals", "/boxscores/team/1/position-totals"),
	("/boxscores/player/{player_name:path}/history", "/boxscores/player/Josh%20Allen/history"),
	("/boxscores/week/{season}/{week}", "/boxscores/week/2025/5"),
	("/boxscores/week/{season}/{week}", "/boxscores/week/2025/5?include_bench=true"),
	("/boxscores/random-player", "/boxscores/random-player"),
	("/boxscores/position/{position:path}/summary", "/boxscores/position/WR/summary"),
	("/boxscores/position/{position:path}/summary", "/boxscores/position/D%2FST/summary"),
	("/tower/round", "/tower/round"),
	("/tower/daily", "/tower/daily"),
	("/tower/leaderboard", "/tower/leaderboard"),
	("/transactions/seasons", "/transactions/seasons"),
	("/transactions/season/{year}/week/{week}", "/transactions/season/2025/week/5"),
	("/transactions/team/{team_id}/season/{year}", "/transactions/team/1/season/2025"),
	("/transactions/team/{team_id}", "/transactions/team/1"),
	("/transactions/player/{player_name:path}", "/transactions/player/Josh%20Allen"),
	("/transactions/player/{player_name:path}", "/transactions/player/Josh%20Allen?exact=false"),
	("/transactions/records/biggest-bids", "/transactions/records/biggest-bids"),
	("/transactions/records/biggest-losing-bids", "/transactions/records/biggest-losing-bids"),
	("/transactions/records/biggest-overpays", "/transactions/records/biggest-overpays"),
	("/transactions/records/bargains", "/transactions/records/bargains"),
	("/transactions/records/bargains", "/transactions/records/bargains?paid=false&season=2025"),
	("/transactions/records/most-contested", "/transactions/records/most-contested"),
]

# URL -> ("list", first item's keys, minimum length) or ("dict", keys). The minimum is the
# default limit for endpoints that take one (so a changed default fails), else 1.
SHAPES = {
	'/': ("dict", ['message', 'status']),
	'/seasons/': ("list", ['champion', 'highest_pf', 'in_progress', 'sacko', 'season', 'teams'], 1),
	'/seasons/2025/standings': ("list", ['championship', 'draws', 'final_standing', 'in_progress', 'losses', 'most_points', 'owner', 'points_against', 'points_for', 'sacko', 'team_id', 'wins'], 1),
	'/seasons/2026/standings': ("list", ['championship', 'draws', 'final_standing', 'games_played', 'in_progress', 'losses', 'most_points', 'owner', 'points_against', 'points_for', 'sacko', 'team_id', 'through_week', 'wins'], 1),
	'/seasons/2025/weekly-scores': ("list", ['is_playoffs', 'owner', 'score', 'team_id', 'week', 'won'], 1),
	'/seasons/2025/luck': ("list", ['actual_wins', 'expected_wins', 'luck', 'owner', 'team_id'], 1),
	'/seasons/2025/top-scorer': ("dict", ['owner', 'player_name', 'position', 'total_points']),
	'/records/alltime': ("list", ['championships', 'most_points_seasons', 'owner', 'ppg', 'sackos', 'seasons_played', 'team_id', 'total_draws', 'total_losses', 'total_points_against', 'total_points_for', 'total_wins', 'win_pct'], 1),
	'/records/scoring/highest-season': ("list", ['final_standing', 'losses', 'owner', 'points_against', 'points_for', 'season', 'team_id', 'wins'], 10),
	'/records/scoring/lowest-season': ("list", ['final_standing', 'losses', 'owner', 'points_against', 'points_for', 'season', 'team_id', 'wins'], 10),
	'/records/scoring/best-single-week': ("list", ['is_playoffs', 'opp_score', 'opponent', 'owner', 'score', 'season', 'team_id', 'week'], 10),
	'/records/scoring/worst-single-week': ("list", ['is_playoffs', 'opp_score', 'opponent', 'owner', 'score', 'season', 'team_id', 'week'], 10),
	'/records/streaks/wins': ("list", ['end_season', 'end_week', 'owner', 'start_season', 'start_week', 'streak_length', 'team_id'], 10),
	'/records/streaks/losses': ("list", ['end_season', 'end_week', 'owner', 'start_season', 'start_week', 'streak_length', 'team_id'], 10),
	'/records/draft/most-drafted-position': ("list", ['positions', 'round'], 1),
	'/records/draft/by-owner': ("list", ['owner', 'positions', 'team_id'], 1),
	'/records/scoring/highest-losing-scores': ("list", ['is_playoffs', 'opp_score', 'opponent', 'owner', 'score', 'season', 'team_id', 'week'], 10),
	'/records/scoring/lowest-winning-scores': ("list", ['is_playoffs', 'opp_score', 'opponent', 'owner', 'score', 'season', 'team_id', 'week'], 10),
	'/draft/': ("list", ['keeper_picks', 'rounds', 'season', 'total_picks'], 1),
	'/draft/tendencies': ("list", ['owner', 'rounds', 'seasons_played', 'team_id'], 1),
	'/draft/player/Josh%20Allen': ("dict", ['query', 'results', 'times_drafted']),
	'/draft/value/history': ("dict", ['busts', 'steals']),
	'/draft/2025': ("dict", ['rounds', 'season']),
	'/draft/2025/by-team': ("dict", ['season', 'teams']),
	'/draft/2025/keepers': ("dict", ['keepers', 'season']),
	'/draft/2025/value': ("dict", ['bust', 'picks', 'season', 'steal']),
	'/matchups/h2h/1/2': ("dict", ['games', 'playoff_games', 'regular_season_games', 'team_a_id', 'team_a_wins', 'team_b_id', 'team_b_wins', 'ties', 'total_games']),
	'/matchups/season/2025': ("dict", ['season', 'weeks']),
	'/matchups/season/2025/week/5': ("dict", ['games', 'is_playoffs', 'season', 'week']),
	'/matchups/records/highest-scores': ("list", ['is_playoffs', 'opp_score', 'opponent', 'owner', 'score', 'season', 'week'], 10),
	'/matchups/records/biggest-blowouts': ("list", ['away_owner', 'away_score', 'home_owner', 'home_score', 'is_playoffs', 'margin', 'season', 'week', 'winner'], 10),
	'/matchups/records/closest-games': ("list", ['away_owner', 'away_score', 'home_owner', 'home_score', 'is_playoffs', 'margin', 'season', 'week', 'winner'], 10),
	'/matchups/random-h2h': ("dict", ['highlights', 'summary', 'team_a', 'team_b']),
	'/teams/': ("list", ['championships', 'first_season', 'last_season', 'most_points_seasons', 'owner', 'sackos', 'seasons_played', 'team_id', 'total_draws', 'total_losses', 'total_points_against', 'total_points_for', 'total_wins', 'win_pct'], 1),
	'/teams/1': ("dict", ['career', 'h2h', 'owner', 'seasons', 'team_id']),
	'/boxscores/top-performances': ("list", ['is_playoffs', 'is_starter', 'opponent', 'owner', 'player_name', 'points_scored', 'position', 'season', 'team_id', 'week'], 25),
	'/boxscores/top-performances?position=QB': ("list", ['is_playoffs', 'is_starter', 'opponent', 'owner', 'player_name', 'points_scored', 'position', 'season', 'team_id', 'week'], 25),
	'/boxscores/position-breakdown': ("list", ['positions', 'season'], 1),
	'/boxscores/team/1?season=2025': ("dict", ['entries', 'owner', 'team_id']),
	'/boxscores/team/1/best-weeks': ("list", ['is_playoffs', 'is_starter', 'opponent', 'player_name', 'points_scored', 'position', 'season', 'week'], 10),
	'/boxscores/team/1/position-totals': ("list", ['positions', 'season'], 1),
	'/boxscores/player/Josh%20Allen/history': ("dict", ['by_owner', 'query', 'results', 'total_appearances']),
	'/boxscores/week/2025/5': ("dict", ['season', 'teams', 'week']),
	'/boxscores/week/2025/5?include_bench=true': ("dict", ['season', 'teams', 'week']),
	'/boxscores/random-player': ("dict", ['appearances', 'avg_points', 'best_game', 'best_game_detail', 'draft', 'player_name', 'position', 'seasons', 'top_owner', 'total_points', 'worst_game']),
	'/boxscores/position/WR/summary': ("dict", ['bottom_games', 'by_season', 'draft', 'draft_by_season', 'most_drafted', 'most_rostered', 'position', 'scoring', 'top_games']),
	'/boxscores/position/D%2FST/summary': ("dict", ['bottom_games', 'by_season', 'draft', 'draft_by_season', 'most_drafted', 'most_rostered', 'position', 'scoring', 'top_games']),
	'/tower/round': ("dict", ['count', 'questions']),
	'/tower/daily': ("dict", ['count', 'daily_seed', 'questions']),
	'/tower/leaderboard': ("dict", ['daily_seed', 'entries']),
	'/transactions/seasons': ("list", ['faab_spent', 'first_week', 'free_agent_moves', 'has_losing_bids', 'last_week', 'season', 'transactions', 'waiver_claims_won', 'weeks'], 1),
	'/transactions/season/2025/week/5': ("dict", ['season', 'transactions', 'week']),
	'/transactions/team/1/season/2025': ("dict", ['owner', 'season', 'team_id', 'transactions']),
	'/transactions/team/1': ("dict", ['owner', 'seasons', 'team_id']),
	'/transactions/player/Josh%20Allen': ("dict", ['exact', 'players', 'query', 'results', 'times_added', 'times_dropped']),
	'/transactions/player/Josh%20Allen?exact=false': ("dict", ['exact', 'players', 'query', 'results', 'times_added', 'times_dropped']),
	'/transactions/records/biggest-bids': ("list", ['bid_amount', 'event_at', 'owner', 'player_name', 'position', 'season', 'team_id', 'week'], 25),
	'/transactions/records/biggest-losing-bids': ("list", ['bid_amount', 'event_at', 'owner', 'player_name', 'position', 'season', 'status', 'team_id', 'week', 'winner_owner', 'winning_bid'], 25),
	'/transactions/records/biggest-overpays': ("list", ['bid_amount', 'event_at', 'overpay', 'owner', 'player_name', 'position', 'runner_up_bid', 'runner_up_owner', 'season', 'team_id', 'week'], 25),
	'/transactions/records/bargains': ("list", ['bid_amount', 'event_at', 'games', 'owner', 'player_name', 'points', 'points_per_dollar', 'position', 'ppg', 'season', 'team_id', 'type', 'week'], 25),
	'/transactions/records/bargains?paid=false&season=2025': ("list", ['bid_amount', 'event_at', 'games', 'owner', 'player_name', 'points', 'points_per_dollar', 'position', 'ppg', 'season', 'team_id', 'type', 'week'], 25),
	'/transactions/records/most-contested': ("list", ['bids', 'claims', 'espn_player_id', 'player_name', 'position', 'run_date', 'season', 'teams', 'week', 'winner_owner', 'winning_bid'], 25),
}


def shape_problem(body, expected):
	"""None if body matches the expected shape, else a description of the difference."""
	kind, keys = expected[0], expected[1]
	if kind == "dict":
		if not isinstance(body, dict):
			return f"expected an object, got {type(body).__name__}"
		got = sorted(body)
	else:
		if not isinstance(body, list):
			return f"expected a list, got {type(body).__name__}"
		if len(body) < expected[2]:
			return f"expected at least {expected[2]} items, got {len(body)}"
		if not body:
			return None
		got = sorted(body[0]) if isinstance(body[0], dict) else type(body[0]).__name__
	if got != keys:
		return f"keys differ: missing {sorted(set(keys) - set(got))}, extra {sorted(set(got) - set(keys))}"
	return None


@unittest.skipUnless(API_URL, "API_DATABASE_URL not set in .env")
class ApiSmoke(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		with socket.socket() as s:
			s.bind(("127.0.0.1", 0))
			port = s.getsockname()[1]
		cls.base = f"http://127.0.0.1:{port}"
		cls.server_log = tempfile.TemporaryFile()
		cls.server = subprocess.Popen(
			[sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
			 "--root-path", "/api", "--no-server-header"],
			cwd=ROOT, stdout=cls.server_log, stderr=subprocess.STDOUT)
		deadline = time.time() + 30
		while True:
			try:
				urllib.request.urlopen(cls.base + "/", timeout=2).close()
				break
			except (urllib.error.URLError, ConnectionError):
				if time.time() > deadline or cls.server.poll() is not None:
					cls.tearDownClass()
					raise RuntimeError("test API server did not start")
				time.sleep(0.3)

	@classmethod
	def tearDownClass(cls):
		cls.server.terminate()
		cls.server.wait(timeout=10)
		cls.server_log.close()

	def server_tail(self):
		self.server_log.seek(0)
		return self.server_log.read().decode(errors="replace")[-1500:]

	def test_connects_as_rockwood_api(self):
		import asyncpg

		async def who():
			conn = await asyncpg.connect(API_URL, ssl=False)
			try:
				return await conn.fetchval("SELECT current_user")
			finally:
				await conn.close()
		self.assertEqual(asyncio.run(who()), "rockwood_api")

	def test_every_get_route_has_a_case(self):
		from fastapi.routing import APIRoute
		from app.main import app
		routes = {r.path for r in app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
		covered = {template for template, _ in CASES}
		self.assertEqual(sorted(routes - covered), [], "GET routes with no smoke-test case")
		self.assertEqual(sorted(covered - routes), [], "cases for routes that no longer exist")

	def test_routes(self):
		for template, url in CASES:
			with self.subTest(url=url):
				try:
					with urllib.request.urlopen(self.base + url, timeout=60) as r:
						status, body = r.status, json.load(r)
				except urllib.error.HTTPError as e:
					self.fail(f"{url} -> {e.code}\n{self.server_tail()}")
				self.assertEqual(status, 200)
				self.assertIn(url, SHAPES, "no SHAPES entry")
				problem = shape_problem(body, SHAPES[url])
				self.assertIsNone(problem, f"{url}: {problem}")


if __name__ == "__main__":
	unittest.main()
