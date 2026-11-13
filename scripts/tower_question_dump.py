#!/usr/bin/env python
"""
tower_question_dump.py - review dump of The Tower's questions. Read-only (connects as rockwood_api).

Builds N rounds (default 500) with the router's round builder, seeded "dump-0" .. "dump-N", writes every
unique question (options, answer, removal order, flavor) to a text file grouped by leaderboard and
framing, and prints counts per leaderboard and framing. Asserts the question invariants: three distinct
distractors, the answer not among them, no option tied with the answer, eliminate = two distinct
distractors; and per round: no repeated id, at most two questions per leaderboard.

	venv\\Scripts\\python.exe scripts\\tower_question_dump.py [--rounds 500] [--out docs\\tower_question_dump.txt]
"""

import argparse
import asyncio
import logging
import os
import random
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg
from dotenv import load_dotenv

load_dotenv()

from app.question_bank import QUESTION_GENERATORS
from app.routers.tower import _build_round, _tier, round_problems

FRAMINGS = ("most", "fewest", "pick4", "odd")


class CountFailures(logging.Handler):
	def __init__(self):
		super().__init__(logging.WARNING)
		self.failures = Counter()
		self.first = {}

	def emit(self, record):
		name = record.args[0] if record.args else record.getMessage()
		self.failures[name] += 1
		self.first.setdefault(name, record.exc_text or (record.exc_info and logging.Formatter().formatException(record.exc_info)))


async def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--rounds", type=int, default=500)
	parser.add_argument("--count", type=int, default=10)
	parser.add_argument("--out", default=os.path.join("docs", "tower_question_dump.txt"))
	args = parser.parse_args()

	failures = CountFailures()
	logging.getLogger("app.tower").addHandler(failures)
	logging.getLogger("app.tower").propagate = False

	db = await asyncpg.connect(os.environ["API_DATABASE_URL"], ssl=False)
	await db.execute("SET default_transaction_read_only = on")

	unique = {}
	seen_count = Counter()
	problems = []
	off_tier = Counter()
	short_rounds = 0
	started = time.perf_counter()
	for n in range(args.rounds):
		questions = await _build_round(db, random.Random(f"dump-{n}"), args.count)
		if len(questions) < args.count:
			short_rounds += 1
		problems += [f"round {n}: {b}" for b in round_problems(questions)]
		for i, q in enumerate(questions):
			if q["difficulty"] != _tier(i, args.count):
				off_tier[(_tier(i, args.count), q["difficulty"])] += 1
			unique.setdefault(q["id"], q)
			seen_count[q["id"]] += 1
	elapsed = time.perf_counter() - started
	await db.close()

	by_group = defaultdict(list)
	for q in unique.values():
		by_group[(q["board"], q["framing"])].append(q)

	with open(args.out, "w", encoding="utf-8") as f:
		f.write(f"The Tower question dump: {args.rounds} rounds of {args.count}, {len(unique)} unique questions\n")
		f.write("Options in served order; * = answer; eliminate = removal order (first at 12s, second at 18s)\n")
		for (board, framing), qs in sorted(by_group.items()):
			f.write(f"\n{'=' * 100}\n{board} / {framing}  ({len(qs)} unique)\n{'=' * 100}\n")
			for q in sorted(qs, key=lambda q: q["id"]):
				f.write(f"\n[{q['id']}]  difficulty {q['difficulty']}, {q['category']}, served {seen_count[q['id']]}x\n")
				f.write(f"  Q: {q['question']}\n")
				for o in q["options"]:
					f.write(f"     {'*' if o == q['answer'] else ' '} {o}\n")
				f.write(f"  eliminate: {' -> '.join(q['eliminate'])}\n")
				f.write(f"  flavor: {q['flavor']}\n")

	# Summary
	print(f"{args.rounds} rounds in {elapsed:.1f}s ({elapsed / args.rounds * 1000:.0f} ms/round), "
	      f"{len(unique)} unique questions -> {args.out}")
	print(f"short rounds: {short_rounds}")
	print("\nleaderboard / framing                     unique  served")
	served = Counter()
	for q_id, n in seen_count.items():
		q = unique[q_id]
		served[(q["board"], q["framing"])] += n
	for gen_board in sorted({g.base for g in QUESTION_GENERATORS}):
		for framing in FRAMINGS:
			key = (gen_board, framing)
			registered = any(g.base == gen_board and g.framing.__name__.replace("pick_of_four", "pick4")
			                 .replace("odd_one_out", "odd") == framing for g in QUESTION_GENERATORS)
			if registered:
				flag = "" if key in by_group else "   <- never produced"
				print(f"  {gen_board + ' / ' + framing:40} {len(by_group.get(key, [])):6} {served[key]:7}{flag}")
	print(f"\nby difficulty (served): {dict(sorted(Counter(unique[i]['difficulty'] for i in seen_count.elements()).items()))}")
	print(f"slots filled from another tier (planned, got): {dict(off_tier) or 'none'}")
	print(f"generator exceptions: {dict(failures.failures) or 'none'}")
	for name, tb in failures.first.items():
		print(f"\n--- first exception in {name}\n{tb}")
	if problems:
		print(f"\nINVARIANT FAILURES ({len(problems)}):")
		for p in problems[:50]:
			print("  ", p)
		sys.exit(1)
	print("\ninvariants: all hold")


if __name__ == "__main__":
	asyncio.run(main())
