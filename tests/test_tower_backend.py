r"""
The Tower backend: the daily cache, keep-best scores and the POST /tower/score rate limiter.

The database tests connect as rockwood_api and run inside a transaction that is always rolled back, so
they leave nothing behind (the daily uses a far-future date, the scores a test-only name).

	venv\Scripts\python.exe -B -m unittest tests.test_tower_backend -v

The database tests are skipped when .env has no API_DATABASE_URL.
"""

import asyncio
import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

API_URL = dotenv_values(os.path.join(ROOT, ".env")).get("API_DATABASE_URL")
os.environ.setdefault("API_DATABASE_URL", API_URL or "unset")   # app.database refuses to import without it

from fastapi import HTTPException  # noqa: E402

from app.routers import tower  # noqa: E402

TEST_DATE = date(2099, 1, 1)
TEST_NAME = "zz tower test"


class Rollback(Exception):
	pass


def in_rolled_back_transaction(work):
	"""Run work(conn) as rockwood_api inside a transaction that is rolled back; return its result."""
	import asyncpg

	async def run():
		conn = await asyncpg.connect(API_URL, ssl=False)
		result = None
		try:
			async with conn.transaction():
				result = await work(conn)
				raise Rollback
		except Rollback:
			return result
		finally:
			await conn.close()
	return asyncio.run(run())


@unittest.skipUnless(API_URL, "no API_DATABASE_URL in .env")
class DailyCache(unittest.TestCase):
	def test_same_payload_every_call(self):
		async def work(conn):
			tower._daily_memo.clear()
			first = await tower.daily_round(conn, TEST_DATE)       # generates and stores
			memo = await tower.daily_round(conn, TEST_DATE)        # in-memory copy
			tower._daily_memo.clear()                               # as after an API restart
			stored = await tower.daily_round(conn, TEST_DATE)      # read from tower_daily
			rows = await conn.fetchval("SELECT count(*) FROM tower_daily WHERE daily_seed = $1", TEST_DATE)
			tower._daily_memo.clear()
			return first, memo, stored, rows
		first, memo, stored, rows = in_rolled_back_transaction(work)
		self.assertEqual(len(first), tower.DAILY_COUNT)
		self.assertEqual(first, memo)
		self.assertEqual(first, stored)
		self.assertEqual(rows, 1)
		self.assertTrue(all(len(q["eliminate"]) == 2 and "values" not in q for q in first))

	def test_stored_row_wins(self):
		"""A row already in tower_daily is served as is, even if generation would now differ."""
		async def work(conn):
			tower._daily_memo.clear()
			await conn.execute("INSERT INTO tower_daily (daily_seed, questions) VALUES ($1, $2::jsonb)",
			                   TEST_DATE, '[{"id": "stored"}]')
			served = await tower.daily_round(conn, TEST_DATE)
			tower._daily_memo.clear()
			return served
		self.assertEqual(in_rolled_back_transaction(work), [{"id": "stored"}])


@unittest.skipUnless(API_URL, "no API_DATABASE_URL in .env")
class KeepBest(unittest.TestCase):
	def scores(self, seed):
		async def work(conn):
			results = [
				await tower.save_score(conn, TEST_NAME, 300, 5, 10, seed),
				await tower.save_score(conn, TEST_NAME.upper() + "  ", 200, 4, 10, seed),   # same player, lower
				await tower.save_score(conn, " " + TEST_NAME, 500, 7, 10, seed),           # same player, higher
				await tower.save_score(conn, TEST_NAME, 500, 6, 10, seed),                 # equal: kept
			]
			rows = await conn.fetch("""SELECT player_name, score, questions_correct FROM tower_scores
			                           WHERE lower(trim(player_name)) = $1 AND daily_seed IS NOT DISTINCT FROM $2""",
			                        TEST_NAME, seed)
			return results, [dict(r) for r in rows]
		return in_rolled_back_transaction(work)

	def test_free_play_keeps_higher(self):
		results, rows = self.scores(None)
		self.assertEqual(results, [True, False, True, False])
		self.assertEqual(rows, [{"player_name": TEST_NAME, "score": 500, "questions_correct": 7}])

	def test_daily_keeps_higher(self):
		results, rows = self.scores(TEST_DATE)
		self.assertEqual(results, [True, False, True, False])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["score"], 500)

	def test_boards_are_separate(self):
		async def work(conn):
			await tower.save_score(conn, TEST_NAME, 300, 5, 10, None)
			await tower.save_score(conn, TEST_NAME, 100, 2, 10, TEST_DATE)
			return await conn.fetchval("SELECT count(*) FROM tower_scores WHERE lower(trim(player_name)) = $1",
			                           TEST_NAME)
		self.assertEqual(in_rolled_back_transaction(work), 2)


class Limiter(unittest.TestCase):
	def test_eleventh_request_in_a_minute_is_429(self):
		now = [1000.0]
		limiter = tower.RateLimiter(limit=10, window=60.0, clock=lambda: now[0])
		for _ in range(10):
			limiter.check("203.0.113.5")
		with self.assertRaises(HTTPException) as refused:
			limiter.check("203.0.113.5")
		self.assertEqual(refused.exception.status_code, 429)
		self.assertIn("Retry-After", refused.exception.headers)
		limiter.check("198.51.100.7")              # another client has its own allowance
		now[0] += 60.0
		limiter.check("203.0.113.5")               # the window has moved on



@unittest.skipUnless(API_URL, "no API_DATABASE_URL in .env")
class LimiterRoute(unittest.TestCase):
	"""POST /tower/score through a real uvicorn (as the service runs it), with an empty name so every
	request stops at the 400 before any write. Requests come from 127.0.0.1, which uvicorn trusts, so
	X-Forwarded-For sets the client the way Caddy's does."""

	def test_route_limits_per_forwarded_ip(self):
		import json
		import socket
		import subprocess
		import tempfile
		import time
		import urllib.error
		import urllib.request

		with socket.socket() as s:
			s.bind(("127.0.0.1", 0))
			port = s.getsockname()[1]
		log = tempfile.TemporaryFile()
		server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
		                           "--port", str(port), "--no-server-header"], cwd=ROOT, stdout=log, stderr=log)
		base = f"http://127.0.0.1:{port}"

		def post(ip):
			req = urllib.request.Request(base + "/tower/score", method="POST",
			                             data=json.dumps({"player_name": " ", "score": 0, "correct": 0, "total": 10}).encode(),
			                             headers={"Content-Type": "application/json", "X-Forwarded-For": ip})
			try:
				with urllib.request.urlopen(req, timeout=10) as r:
					return r.status
			except urllib.error.HTTPError as e:
				return e.code
		try:
			deadline = time.time() + 30
			while True:
				try:
					urllib.request.urlopen(base + "/", timeout=2).close()
					break
				except (urllib.error.URLError, ConnectionError):
					if time.time() > deadline or server.poll() is not None:
						raise RuntimeError("test API server did not start")
					time.sleep(0.3)
			codes = [post("203.0.113.5") for _ in range(11)]
			other = post("198.51.100.7")
		finally:
			server.terminate()
			server.wait(timeout=10)
			log.close()
		self.assertEqual(codes[:10], [400] * 10)
		self.assertEqual(codes[10], 429)
		self.assertEqual(other, 400)


if __name__ == "__main__":
	unittest.main()
