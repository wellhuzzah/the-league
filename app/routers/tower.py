"""
tower.py — The Tower quiz router

Endpoints:
  GET  /tower/round?count=10        → returns N deduplicated questions
  GET  /tower/daily                 → deterministic daily-challenge round
  POST /tower/score                 → stores a completed round result
  GET  /tower/leaderboard?limit=20  → top scores

Place this file at: app/routers/tower.py
Register in app/main.py: app.include_router(tower.router)

Dependencies: question_bank.py must be at app/question_bank.py
"""

import hashlib
import random
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from app.database import get_pool
from app.question_bank import QUESTION_GENERATORS

router = APIRouter(prefix="/tower", tags=["tower"])


# ---------------------------------------------------------------------------
# Shared context loader — fetches seasons + owners once per request
# ---------------------------------------------------------------------------

async def _load_context(db):
	"""Load seasons list and owner names for use by question generators."""
	season_rows = await db.fetch("SELECT DISTINCT season FROM records ORDER BY season")
	seasons = [int(r["season"]) for r in season_rows]

	owner_rows = await db.fetch("SELECT DISTINCT owner FROM teams ORDER BY owner")
	owners = [r["owner"] for r in owner_rows]

	return seasons, owners


# ---------------------------------------------------------------------------
# Round builder — shared by /round and /daily
# ---------------------------------------------------------------------------

async def _build_round(db, count: int):
	"""
	Returns `count` deduplicated questions sampled randomly from all generators.
	Shuffles answer options so correct answer isn't always in the same position.
	"""
	seasons, owners = await _load_context(db)

	questions = []
	seen_ids = set()
	generators = QUESTION_GENERATORS.copy()
	random.shuffle(generators)

	# attempt each generator; retry shuffled pool until we have enough or exhaust
	attempts = 0
	gen_index = 0
	while len(questions) < count and attempts < count * 6:
		generator = generators[gen_index % len(generators)]
		gen_index += 1
		attempts += 1

		try:
			result = await generator(db, seasons, owners)
		except Exception:
			continue

		if result is None:
			continue
		if result["id"] in seen_ids:
			continue

		seen_ids.add(result["id"])

		# shuffle options so correct answer is random position
		options = [result["answer"]] + result["distractors"][:3]
		random.shuffle(options)

		questions.append({
			"id": result["id"],
			"category": result["category"],
			"difficulty": result["difficulty"],
			"question": result["question"],
			"options": options,
			"answer": result["answer"],
			"flavor": result["flavor"],
		})

	if not questions:
		raise HTTPException(status_code=500, detail="Could not generate questions")

	return questions


# ---------------------------------------------------------------------------
# GET /tower/round
# ---------------------------------------------------------------------------

@router.get("/round")
async def get_round(count: int = Query(default=10, ge=1, le=20)):
	async with (await get_pool()).acquire() as db:
		questions = await _build_round(db, count)
		return {
			"count": len(questions),
			"questions": questions,
		}


# ---------------------------------------------------------------------------
# GET /tower/daily
# ---------------------------------------------------------------------------

@router.get("/daily")
async def get_daily(count: int = Query(default=10, ge=1, le=20)):
	"""
	Daily challenge: same round logic, but the RNG is seeded with today's UTC
	date so every player gets the same questions on the same day.
	"""
	daily_seed = datetime.now(timezone.utc).date().isoformat()
	async with (await get_pool()).acquire() as db:
		# some generators use SQL-level random() — seed Postgres's RNG on this
		# connection too, otherwise the daily round isn't deterministic
		pg_seed = int(hashlib.sha256(daily_seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 2 - 1
		await db.execute("SELECT setseed($1)", pg_seed)
		random.seed(daily_seed)
		try:
			questions = await _build_round(db, count)
		finally:
			random.seed(None)
			await db.execute("SELECT setseed($1)", random.uniform(-1, 1))
		return {
			"count": len(questions),
			"questions": questions,
			"daily_seed": daily_seed,
		}


# ---------------------------------------------------------------------------
# POST /tower/score
# ---------------------------------------------------------------------------

class ScoreSubmission(BaseModel):
	player_name: str
	score: int
	correct: int
	total: int
	daily_seed: Optional[str] = None  # ISO date string e.g. "2025-05-21", null for free play


def _parse_seed(value: Optional[str]) -> Optional[date]:
	"""daily_seed column is DATE — asyncpg needs a date object, not a string."""
	if value is None:
		return None
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise HTTPException(status_code=400, detail="daily_seed must be an ISO date")


@router.post("/score")
async def submit_score(payload: ScoreSubmission):
	"""Store a completed round result."""
	if not payload.player_name.strip():
		raise HTTPException(status_code=400, detail="player_name required")

	seed = _parse_seed(payload.daily_seed)
	async with (await get_pool()).acquire() as db:
		await db.execute(
			"""
			INSERT INTO tower_scores (player_name, score, questions_correct, total_questions, daily_seed, played_at)
			VALUES ($1, $2, $3, $4, $5, $6)
			""",
			payload.player_name.strip()[:32],
			payload.score,
			payload.correct,
			payload.total,
			seed,
			datetime.now(timezone.utc),
		)

	return {"status": "ok"}


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
	Omit daily_seed for all-time free play leaderboard.
	"""
	seed = _parse_seed(daily_seed)
	async with (await get_pool()).acquire() as db:
		if seed:
			rows = await db.fetch(
				"""
				SELECT player_name, score, questions_correct, total_questions, played_at
				FROM tower_scores
				WHERE daily_seed = $1
				ORDER BY score DESC, played_at ASC
				LIMIT $2
				""",
				seed,
				limit,
			)
		else:
			rows = await db.fetch(
				"""
				SELECT player_name, score, questions_correct, total_questions, played_at
				FROM tower_scores
				WHERE daily_seed IS NULL
				ORDER BY score DESC, played_at ASC
				LIMIT $1
				""",
				limit,
			)

		return {
			"daily_seed": daily_seed,
			"entries": [
				{
					"rank": i + 1,
					"player_name": r["player_name"],
					"score": r["score"],
					"correct": r["questions_correct"],
					"total": r["total_questions"],
					"played_at": r["played_at"].isoformat(),
				}
				for i, r in enumerate(rows)
			],
		}
