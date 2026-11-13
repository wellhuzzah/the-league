"""
tower.py — The Tower quiz router

Endpoints:
  GET  /tower/round?count=10        → N questions, easiest first, each with options and eliminate
  GET  /tower/daily                 → the day's 10 questions, stored in tower_daily on first request
  POST /tower/score                 → keeps a player's best score per board; 10 a minute per client IP
  GET  /tower/leaderboard?limit=20  → top scores

Questions come from app/question_bank.py. Answers ship to the client (no server-side checking).
"""

import json
import logging
import math
import random
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.database import get_pool
from app.params import top_n
from app.question_bank import QUESTION_GENERATORS, Context

router = APIRouter(prefix="/tower", tags=["tower"])
log = logging.getLogger("app.tower")


# ---------------------------------------------------------------------------
# Round builder: shared by /round and /daily
# ---------------------------------------------------------------------------

# A round climbs: the first 30% of questions are difficulty 1, the next 40% difficulty 2, the rest 3
# (10 questions: 1-3, 4-7, 8-10). A short tier fills from the adjacent one.
TIER_BREAKS = (0.3, 0.7)
FALLBACK = {1: (1, 2, 3), 2: (2, 1, 3), 3: (3, 2, 1)}
MAX_PER_BOARD = 2   # questions from one leaderboard per round
PASSES = 3          # times through a tier: a season/owner/player generator can give a new question


def _tier(i, count):
	return 1 if i < TIER_BREAKS[0] * count else 2 if i < TIER_BREAKS[1] * count else 3


async def _build_round(db, rng: random.Random, count: int):
	"""
	`count` questions, easiest first, each with its options shuffled and the two distractors the client
	removes as the timer runs (eliminate, least plausible first). No repeated id, at most MAX_PER_BOARD
	from one leaderboard. All randomness comes from rng (never the module-global random) and every query
	is fully ordered, so a seeded rng gives the same round for the same data.
	Questions keep "values" (option -> leaderboard value) for the dump and tests; public() drops it.
	"""
	ctx = Context(db)
	pools = {}
	for d in (1, 2, 3):
		pools[d] = [g for g in QUESTION_GENERATORS if g.difficulty == d]
		rng.shuffle(pools[d])
	cursor = Counter()
	seen, per_board = set(), Counter()

	async def take(d):
		pool = pools[d]
		while cursor[d] < len(pool) * PASSES:
			gen = pool[cursor[d] % len(pool)]
			cursor[d] += 1
			if per_board[gen.base] >= MAX_PER_BOARD:
				continue
			try:
				q = await gen(rng, ctx)
			except Exception:
				log.warning("Tower generator %s failed", gen.name, exc_info=True)
				continue
			if q is not None and q["id"] not in seen:
				return q
		return None

	questions = []
	for i in range(count):
		q = None
		for d in FALLBACK[_tier(i, count)]:
			q = await take(d)
			if q:
				break
		if q is None:
			break
		seen.add(q["id"])
		per_board[q["board"]] += 1
		options = [q["answer"], *q["distractors"]]
		rng.shuffle(options)
		questions.append({
			"id": q["id"],
			"board": q["board"],
			"framing": q["framing"],
			"category": q["category"],
			"difficulty": q["difficulty"],
			"question": q["question"],
			"options": options,
			"answer": q["answer"],
			"eliminate": q["eliminate"],
			"flavor": q["flavor"],
			"values": q["values"],
		})

	if not questions:
		raise HTTPException(status_code=500, detail="Could not generate questions")

	return questions


def round_problems(questions):
	"""Every broken invariant in a built round, as text (empty = fine): four distinct options with the
	answer among them, no option tied with the answer on its leaderboard, eliminate = two distinct
	distractors, no repeated id, at most MAX_PER_BOARD questions per leaderboard. Used by
	scripts/tower_question_dump.py and tests/test_tower_questions.py."""
	bad = []
	ids = [q["id"] for q in questions]
	if len(ids) != len(set(ids)):
		bad.append("repeated id in the round")
	for board, n in Counter(q["board"] for q in questions).items():
		if n > MAX_PER_BOARD:
			bad.append(f"{n} questions from {board}")
	for q in questions:
		answer, options, values = q["answer"], q["options"], q["values"]
		distractors = [o for o in options if o != answer]
		if len(options) != 4 or len(set(options)) != 4 or answer not in options:
			bad.append(f"{q['id']}: options are not four distinct names including the answer")
		elif any(values[d] == values[answer] for d in distractors):
			bad.append(f"{q['id']}: an option is tied with the answer")
		if len(set(q["eliminate"])) != 2 or not set(q["eliminate"]) <= set(distractors):
			bad.append(f"{q['id']}: eliminate is not two distinct distractors")
	return bad


def public(questions):
	"""Questions as the API serves them (no leaderboard values)."""
	return [{k: v for k, v in q.items() if k != "values"} for q in questions]


# ---------------------------------------------------------------------------
# GET /tower/round
# ---------------------------------------------------------------------------

@router.get("/round")
async def get_round(count: int = Query(default=10, ge=1, le=20)):
	async with (await get_pool()).acquire() as db:
		questions = public(await _build_round(db, random.Random(), count))
		return {
			"count": len(questions),
			"questions": questions,
		}


# ---------------------------------------------------------------------------
# GET /tower/daily
# ---------------------------------------------------------------------------

DAILY_COUNT = 10
_daily_memo = {}   # one entry: {date: questions}, in front of tower_daily


async def daily_round(db, seed: date):
	"""
	The daily round for a UTC date, identical for everyone all day: across API restarts and the
	Thursday import too, because the first request of the day stores it in tower_daily and every later
	one reads that row. Concurrent first requests both generate; ON CONFLICT DO NOTHING keeps the first
	insert and both serve the row read back.
	"""
	if seed in _daily_memo:
		return _daily_memo[seed]
	stored = await db.fetchval("SELECT questions FROM tower_daily WHERE daily_seed = $1", seed)
	if stored is None:
		questions = public(await _build_round(db, random.Random(seed.isoformat()), DAILY_COUNT))
		await db.execute(
			"INSERT INTO tower_daily (daily_seed, questions) VALUES ($1, $2::jsonb) ON CONFLICT (daily_seed) DO NOTHING",
			seed, json.dumps(questions))
		stored = await db.fetchval("SELECT questions FROM tower_daily WHERE daily_seed = $1", seed)
	questions = json.loads(stored)
	_daily_memo.clear()
	_daily_memo[seed] = questions
	return questions


@router.get("/daily")
async def get_daily():
	"""Daily challenge: DAILY_COUNT questions, the same for every player on a UTC date."""
	seed = datetime.now(timezone.utc).date()
	async with (await get_pool()).acquire() as db:
		questions = await daily_round(db, seed)
	return {
		"count": len(questions),
		"questions": questions,
		"daily_seed": seed.isoformat(),
	}


# ---------------------------------------------------------------------------
# POST /tower/score
# ---------------------------------------------------------------------------

MAX_QUESTIONS = 20         # /round and /daily cap count at 20
MIN_POINTS_PER_Q = 10      # Tower.jsx COUNTDOWN: a correct answer is worth 100 falling to 10, never less
MAX_POINTS_PER_Q = 100


class ScoreSubmission(BaseModel):
	"""Bounds and a plausibility check only; a forged but plausible score still passes.
	Anything out of range is a 422 (before this, a huge int reached Postgres and gave a 500)."""
	player_name: str
	score: int = Field(ge=0, le=MAX_QUESTIONS * MAX_POINTS_PER_Q)
	correct: int = Field(ge=0, le=MAX_QUESTIONS)
	total: int = Field(ge=1, le=MAX_QUESTIONS)
	daily_seed: Optional[str] = None  # ISO date string e.g. "2025-05-21", null for free play

	@field_validator("daily_seed")
	@classmethod
	def seed_is_today_or_yesterday(cls, value: Optional[str]) -> Optional[str]:
		"""Today's UTC challenge, or yesterday's for a round finished just after midnight UTC."""
		if value is None:
			return None
		try:
			seed = date.fromisoformat(value)
		except ValueError:
			raise ValueError("daily_seed must be an ISO date")
		today = datetime.now(timezone.utc).date()
		if seed not in (today, today - timedelta(days=1)):
			raise ValueError("daily_seed must be today's or yesterday's UTC date")
		return value

	@model_validator(mode="after")
	def plausible(self):
		if self.correct > self.total:
			raise ValueError("correct can't exceed total")
		low, high = MIN_POINTS_PER_Q * self.correct, MAX_POINTS_PER_Q * self.correct
		if not low <= self.score <= high:
			raise ValueError(f"score must be between {low} and {high} for {self.correct} correct")
		return self


def _parse_seed(value: Optional[str]) -> Optional[date]:
	"""daily_seed column is DATE — asyncpg needs a date object, not a string."""
	if value is None:
		return None
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise HTTPException(status_code=400, detail="daily_seed must be an ISO date")


class RateLimiter:
	"""At most `limit` requests per `window` seconds per client, in memory (the API is one process).
	A refused request doesn't count against the window."""

	def __init__(self, limit=10, window=60.0, clock=time.monotonic):
		self.limit, self.window, self.clock = limit, window, clock
		self.hits = {}   # client -> deque of request times

	def check(self, client):
		now = self.clock()
		if len(self.hits) > 1000:   # forget idle clients
			self.hits = {c: q for c, q in self.hits.items() if q and q[-1] > now - self.window}
		times = self.hits.setdefault(client, deque())
		while times and times[0] <= now - self.window:
			times.popleft()
		if len(times) >= self.limit:
			retry = max(1, math.ceil(times[0] + self.window - now))
			raise HTTPException(status_code=429, detail="Too many scores; try again in a minute",
			                    headers={"Retry-After": str(retry)})
		times.append(now)


score_limiter = RateLimiter(limit=10, window=60.0)


def limit_scores(request: Request):
	"""10 POST /tower/score a minute per client IP. request.client.host is the real client: uvicorn's
	proxy headers (on by default) take it from Caddy's X-Forwarded-For, trusted only from 127.0.0.1
	(FORWARDED_ALLOW_IPS unset), and Caddy replaces any X-Forwarded-For a client sends. Household
	devices share the WAN IP (hairpin NAT), so they share one allowance."""
	score_limiter.check(request.client.host if request.client else "unknown")


# Keep-best: one row per player per board, a name compared trimmed and case-insensitively. The two
# partial unique indexes (schema_v3.sql section 9) are the conflict targets; a row is replaced only by
# a higher score. RETURNING gives a row when the score was inserted or replaced, none when it was kept.
SAVE_SCORE = """
	INSERT INTO tower_scores (player_name, score, questions_correct, total_questions, daily_seed, played_at)
	VALUES ($1, $2, $3, $4, $5, $6)
	ON CONFLICT {target}
	DO UPDATE SET player_name = EXCLUDED.player_name, score = EXCLUDED.score,
	              questions_correct = EXCLUDED.questions_correct, total_questions = EXCLUDED.total_questions,
	              played_at = EXCLUDED.played_at
	WHERE EXCLUDED.score > tower_scores.score
	RETURNING id
"""
DAILY_TARGET = "(lower(trim(player_name)), daily_seed) WHERE daily_seed IS NOT NULL"
FREE_TARGET = "(lower(trim(player_name))) WHERE daily_seed IS NULL"


async def save_score(db, name, score, correct, total, seed: Optional[date]):
	"""Store a result, keeping the player's best on that board. True when this score is now their best."""
	sql = SAVE_SCORE.format(target=DAILY_TARGET if seed else FREE_TARGET)
	row = await db.fetchrow(sql, name.strip()[:32], score, correct, total, seed, datetime.now(timezone.utc))
	return row is not None


@router.post("/score", dependencies=[Depends(limit_scores)])
async def submit_score(payload: ScoreSubmission):
	"""Store a completed round result: the player's best score per board (each daily, and free play)."""
	if not payload.player_name.strip():
		raise HTTPException(status_code=400, detail="player_name required")

	seed = _parse_seed(payload.daily_seed)
	async with (await get_pool()).acquire() as db:
		best = await save_score(db, payload.player_name, payload.score, payload.correct, payload.total, seed)

	return {"status": "ok", "best": best}


# ---------------------------------------------------------------------------
# GET /tower/leaderboard
# ---------------------------------------------------------------------------

@router.get("/leaderboard")
async def get_leaderboard(
	limit: int = Query(default=20, ge=1, le=100),
	daily_seed: Optional[str] = Query(default=None),
):
	"""
	Returns top scores. Pass daily_seed to filter to a specific day's challenge.
	Omit daily_seed for all-time free play leaderboard. Equal scores share a rank, and every score
	tied with the last place is included (league rule), so there can be more than `limit` entries.
	"""
	seed = _parse_seed(daily_seed)
	scores = """
		SELECT player_name, score, questions_correct, total_questions, played_at
		FROM tower_scores
		WHERE daily_seed IS NOT DISTINCT FROM $1
	"""
	async with (await get_pool()).acquire() as db:
		rows = await db.fetch(top_n(scores, "score DESC", "score DESC, played_at ASC", "$2"), seed, limit)

		return {
			"daily_seed": daily_seed,
			"entries": [
				{
					"rank": r["place"],
					"player_name": r["player_name"],
					"score": r["score"],
					"correct": r["questions_correct"],
					"total": r["total_questions"],
					"played_at": r["played_at"].isoformat(),
				}
				for r in rows
			],
		}
