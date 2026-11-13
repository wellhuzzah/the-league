r"""
The Tower question bank: invariants over a few hundred generated questions, the round shape, and
determinism. Read-only, against the database as rockwood_api.

	venv\Scripts\python.exe -B -m unittest tests.test_tower_questions -v

Skipped when .env has no API_DATABASE_URL.
"""

import asyncio
import os
import random
import re
import sys
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402

API_URL = dotenv_values(os.path.join(ROOT, ".env")).get("API_DATABASE_URL")
os.environ.setdefault("API_DATABASE_URL", API_URL or "unset")   # app.database refuses to import without it

from app.question_bank import QUESTION_GENERATORS  # noqa: E402
from app.routers.tower import MAX_PER_BOARD, _build_round, _tier, round_problems  # noqa: E402

ROUNDS = 30   # 300 questions


def build(seeds, count=10):
	import asyncpg

	async def run():
		conn = await asyncpg.connect(API_URL, ssl=False)
		try:
			await conn.execute("SET default_transaction_read_only = on")
			return [await _build_round(conn, random.Random(seed), count) for seed in seeds]
		finally:
			await conn.close()
	return asyncio.run(run())


class NoSqlRandom(unittest.TestCase):
	def test_no_random_in_sql(self):
		"""All randomness goes through the passed rng: no RANDOM()/setseed in the Tower's SQL."""
		for path in ("app/question_bank.py", "app/routers/tower.py"):
			with open(os.path.join(ROOT, path), encoding="utf-8") as f:
				src = f.read()
			sql = [b for b in re.findall(r'"""(.*?)"""', src, re.S) if re.search(r"\bSELECT\b", b)]
			self.assertTrue(sql or path.endswith("tower.py"), f"{path}: no SQL found to check")
			for block in sql:
				self.assertIsNone(re.search(r"\b(random|setseed)\s*\(", block, re.I), f"{path}: {block[:80]}")

	def test_registry(self):
		self.assertEqual({g.difficulty for g in QUESTION_GENERATORS}, {1, 2, 3})
		names = [g.name for g in QUESTION_GENERATORS]
		self.assertEqual(len(names), len(set(names)), "a leaderboard/framing/scope registered twice")


@unittest.skipUnless(API_URL, "no API_DATABASE_URL in .env")
class Rounds(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.rounds = build([f"test-{n}" for n in range(ROUNDS)])

	def test_invariants(self):
		for n, questions in enumerate(self.rounds):
			self.assertEqual(round_problems(questions), [], f"round {n}")

	def test_full_rounds(self):
		self.assertTrue(all(len(r) == 10 for r in self.rounds))

	def test_round_climbs(self):
		"""1-3 difficulty 1, 4-7 difficulty 2, 8-10 difficulty 3 (the pools are deep enough today)."""
		for questions in self.rounds:
			self.assertEqual([q["difficulty"] for q in questions], [_tier(i, 10) for i in range(10)])

	def test_board_cap(self):
		for questions in self.rounds:
			self.assertLessEqual(max(Counter(q["board"] for q in questions).values()), MAX_PER_BOARD)

	def test_eliminate_least_plausible_first(self):
		"""Outside odd one out, the first removed distractor is at least as far from the answer's value
		as the second, and the one left is no further than either."""
		for questions in self.rounds:
			for q in questions:
				if q["framing"] == "odd":
					continue
				dist = {o: abs(float(q["values"][o]) - float(q["values"][q["answer"]])) for o in q["options"]}
				first, second = q["eliminate"]
				kept = next(o for o in q["options"] if o != q["answer"] and o not in q["eliminate"])
				self.assertGreaterEqual(dist[first], dist[second], q["id"])
				self.assertLessEqual(dist[kept], dist[second], q["id"])

	def test_same_seed_same_round(self):
		again = build(["test-0", "test-0"])
		self.assertEqual(again[0], again[1])
		self.assertEqual(again[0], self.rounds[0])


if __name__ == "__main__":
	unittest.main()
