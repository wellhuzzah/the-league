"""
question_bank.py — The Tower question generators

Each generator is an async function that:
  - accepts a db connection (asyncpg)
  - queries real data
  - returns a dict matching the QuestionResult shape, or None if no data found

QuestionResult shape:
{
    "id": str,           # stable unique key, used for deduplication
    "category": str,     # "season" | "scoring" | "draft" | "player" | "h2h" | "streak"
    "difficulty": int,   # 1 (easy) 2 (medium) 3 (hard)
    "question": str,
    "answer": str,
    "distractors": list[str],   # always exactly 3 wrong answers
    "flavor": str,              # shown after reveal — context/color
}

All generators are registered in QUESTION_GENERATORS at the bottom of this file.
The router imports that list and samples from it randomly.
"""

import random
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _other_owners(all_owners: list[str], exclude: str, n: int = 3) -> list[str]:
	"""Pick n distinct owners from all_owners that are not exclude."""
	pool = [o for o in all_owners if o != exclude]
	return random.sample(pool, min(n, len(pool)))


def _numeric_distractors(value: float, n: int = 3, spread: float = 0.12) -> list[str]:
	"""
	Generate n plausible numeric distractors around value.
	spread: fraction of value to vary by (default ±12%)
	"""
	results = set()
	attempts = 0
	while len(results) < n and attempts < 50:
		delta = random.uniform(-spread, spread)
		candidate = round(value * (1 + delta), 1)
		if candidate != value:
			results.add(candidate)
		attempts += 1
	return [str(r) for r in results]


def _year_distractors(year: int, all_seasons: list[int], n: int = 3) -> list[str]:
	"""Pick n seasons adjacent to year as distractors."""
	pool = [s for s in all_seasons if s != year]
	# prefer nearby years
	pool.sort(key=lambda s: abs(s - year))
	return [str(s) for s in pool[:n]]


# ---------------------------------------------------------------------------
# Category: Season Outcomes
# ---------------------------------------------------------------------------

async def q_championship_winner(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who won the championship in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT t.owner FROM records r
		JOIN teams t ON r.team_id = t.team_id
		WHERE r.season = $1 AND r.championship = TRUE
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["owner"]
	return {
		"id": f"champ_{season}",
		"category": "season",
		"difficulty": 2,
		"question": f"Who won the championship in the {season} season?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{season} champion — {answer}",
	}


async def q_sacko_winner(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who won the sacko in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT t.owner FROM records r
		JOIN teams t ON r.team_id = t.team_id
		WHERE r.season = $1 AND r.sacko = TRUE
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["owner"]
	return {
		"id": f"sacko_{season}",
		"category": "season",
		"difficulty": 2,
		"question": f"Who earned the Sacko in the {season} season?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{season} Sacko — {answer}",
	}


async def q_most_points_season(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who scored the most points in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT t.owner, r.points_for FROM records r
		JOIN teams t ON r.team_id = t.team_id
		WHERE r.season = $1
		ORDER BY r.points_for DESC
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["owner"]
	pts = float(row["points_for"])
	return {
		"id": f"most_pts_season_{season}",
		"category": "season",
		"difficulty": 2,
		"question": f"Who scored the most total points in the {season} season?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{season} — {pts:.1f} points",
	}


async def q_season_wins_leader(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who had the most wins in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT t.owner, r.wins FROM records r
		JOIN teams t ON r.team_id = t.team_id
		WHERE r.season = $1
		ORDER BY r.wins DESC
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["owner"]
	wins = row["wins"]
	return {
		"id": f"wins_leader_{season}",
		"category": "season",
		"difficulty": 2,
		"question": f"Who finished with the most wins in the {season} regular season?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{season} — {wins} wins",
	}


async def q_final_standing(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""What place did owner X finish in year Y?"""
	season = random.choice(seasons)
	# pick a random owner who played that season
	rows = await db.fetch(
		"""
		SELECT t.owner, r.final_standing FROM records r
		JOIN teams t ON r.team_id = t.team_id
		WHERE r.season = $1 AND r.final_standing IS NOT NULL
		""",
		season,
	)
	if not rows:
		return None
	row = random.choice(rows)
	owner = row["owner"]
	standing = row["final_standing"]
	suffix = {1: "1st", 2: "2nd", 3: "3rd"}.get(standing, f"{standing}th")
	# distractors: other plausible standings
	all_standings = [r["final_standing"] for r in rows if r["final_standing"] != standing]
	distractor_standings = random.sample(all_standings, min(3, len(all_standings)))
	distractors = [
		{1: "1st", 2: "2nd", 3: "3rd"}.get(s, f"{s}th") for s in distractor_standings
	]
	return {
		"id": f"standing_{owner.replace(' ', '_')}_{season}",
		"category": "season",
		"difficulty": 3,
		"question": f"Where did {owner} finish in the {season} season?",
		"answer": suffix,
		"distractors": distractors,
		"flavor": f"{owner} finished {suffix} in {season}",
	}


# ---------------------------------------------------------------------------
# Category: Scoring Records
# ---------------------------------------------------------------------------

async def q_highest_single_week(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who scored the most points in a single week in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT t.owner, m.week,
			GREATEST(m.home_score, m.away_score) AS top_score,
			CASE WHEN m.home_score > m.away_score THEN ht.owner ELSE at.owner END AS scorer
		FROM matchups m
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		JOIN teams t ON t.team_id = CASE WHEN m.home_score > m.away_score THEN m.home_team_id ELSE m.away_team_id END
		WHERE m.season = $1 AND m.home_score > 0 AND m.away_score > 0
		ORDER BY top_score DESC
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["owner"]
	score = float(row["top_score"])
	week = row["week"]
	return {
		"id": f"high_week_{season}",
		"category": "scoring",
		"difficulty": 2,
		"question": f"Who scored the most points in a single week during the {season} season?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"Week {week}, {season} — {score:.1f} pts",
	}


async def q_highest_score_exact(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""What was the highest single-week score in year X? (numeric answer)"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT GREATEST(home_score, away_score) AS top_score,
			   m.week,
			   CASE WHEN home_score > away_score THEN ht.owner ELSE at.owner END AS scorer
		FROM matchups m
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		WHERE m.season = $1 AND home_score > 0 AND away_score > 0
		ORDER BY top_score DESC
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	score = float(row["top_score"])
	scorer = row["scorer"]
	week = row["week"]
	answer = f"{score:.1f}"
	return {
		"id": f"high_score_exact_{season}",
		"category": "scoring",
		"difficulty": 3,
		"question": f"What was the highest single-week score in the {season} season? (scored by {scorer}, Week {week})",
		"answer": answer,
		"distractors": _numeric_distractors(score),
		"flavor": f"{scorer}, Week {week}, {season} — {score:.1f} pts",
	}


async def q_closest_game(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who were the two teams in the closest game in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT ht.owner AS home, at.owner AS away, m.margin, m.week
		FROM matchups m
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		WHERE m.season = $1 AND m.home_score > 0 AND m.away_score > 0
		ORDER BY m.margin ASC
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	home = row["home"]
	away = row["away"]
	margin = float(row["margin"])
	answer = f"{home} vs {away}"
	# build plausible distractors from other owners
	others = _other_owners(owners, home, n=6)
	others = [o for o in others if o != away]
	d1 = f"{away} vs {others[0]}" if others else f"{home} vs {others[0]}"
	d2 = f"{others[1]} vs {others[2]}" if len(others) >= 3 else f"{home} vs {others[1]}"
	d3 = f"{others[3]} vs {others[4]}" if len(others) >= 5 else f"{away} vs {others[2]}"
	return {
		"id": f"closest_game_{season}",
		"category": "scoring",
		"difficulty": 3,
		"question": f"Which matchup was the closest game of the {season} season?",
		"answer": answer,
		"distractors": [d1, d2, d3],
		"flavor": f"Week {row['week']}, {season} — margin of {margin:.1f} pts",
	}


# ---------------------------------------------------------------------------
# Category: Draft
# ---------------------------------------------------------------------------

async def q_first_overall_pick(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who had the first overall pick in year X?"""
	# draft data available all seasons
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT t.owner, d.player_name FROM draft_picks d
		JOIN teams t ON d.team_id = t.team_id
		WHERE d.season = $1 AND d.overall_pick = 1
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["owner"]
	player = row["player_name"]
	return {
		"id": f"first_pick_{season}",
		"category": "draft",
		"difficulty": 2,
		"question": f"Who held the first overall pick in the {season} draft?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{season} pick #1 — {player}",
	}


async def q_first_overall_player(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who was taken first overall in year X?"""
	season = random.choice(seasons)
	row = await db.fetchrow(
		"""
		SELECT d.player_name, t.owner FROM draft_picks d
		JOIN teams t ON d.team_id = t.team_id
		WHERE d.season = $1 AND d.overall_pick = 1
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	answer = row["player_name"]
	owner = row["owner"]
	# distractors: other players drafted early that season
	others = await db.fetch(
		"""
		SELECT player_name FROM draft_picks
		WHERE season = $1 AND overall_pick BETWEEN 2 AND 10
		ORDER BY overall_pick
		""",
		season,
	)
	distractor_pool = [r["player_name"] for r in others if r["player_name"] != answer]
	distractors = random.sample(distractor_pool, min(3, len(distractor_pool)))
	if len(distractors) < 3:
		return None
	return {
		"id": f"first_player_{season}",
		"category": "draft",
		"difficulty": 2,
		"question": f"Who was selected with the first overall pick in the {season} draft?",
		"answer": answer,
		"distractors": distractors,
		"flavor": f"{season} #1 overall — {answer} ({owner})",
	}


async def q_draft_position_first_round(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""What position did owner X draft first in year Y?"""
	season = random.choice(seasons)
	rows = await db.fetch(
		"""
		SELECT t.owner, d.position, d.round_num FROM draft_picks d
		JOIN teams t ON d.team_id = t.team_id
		WHERE d.season = $1 AND d.round_num = 1
		ORDER BY d.overall_pick
		""",
		season,
	)
	if not rows:
		return None
	row = random.choice(rows)
	owner = row["owner"]
	answer = row["position"]
	all_positions = ["QB", "RB", "WR", "TE", "K", "D/ST"]
	distractors = [p for p in all_positions if p != answer]
	distractors = random.sample(distractors, min(3, len(distractors)))
	return {
		"id": f"draft_pos_{owner.replace(' ', '_')}_{season}",
		"category": "draft",
		"difficulty": 3,
		"question": f"What position did {owner} select in the first round of the {season} draft?",
		"answer": answer,
		"distractors": distractors,
		"flavor": f"{owner}, {season} Round 1 — {answer}",
	}


# ---------------------------------------------------------------------------
# Category: Player (2018+ only)
# ---------------------------------------------------------------------------

async def q_top_scorer_position_week(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who had the top scoring [position] in week X of year Y? (2018+ only)"""
	box_seasons = [s for s in seasons if s >= 2018]
	if not box_seasons:
		return None
	season = random.choice(box_seasons)
	position = random.choice(["QB", "RB", "WR", "TE"])

	row = await db.fetchrow(
		"""
		SELECT b.player_name, b.points_scored, b.week, t.owner
		FROM box_scores b
		JOIN teams t ON b.team_id = t.team_id
		WHERE b.season = $1 AND b.position = $2
		  AND b.is_starter = TRUE AND b.points_scored > 0
		ORDER BY b.points_scored DESC, b.week, b.team_id, b.espn_player_id
		LIMIT 1
		""",
		season,
		position,
	)
	if not row:
		return None
	answer = row["player_name"]
	pts = float(row["points_scored"])
	week = row["week"]
	owner = row["owner"]
	# distractors: other top players at that position that season
	others = await db.fetch(
		"""
		SELECT DISTINCT player_name FROM box_scores
		WHERE season = $1 AND position = $2 AND is_starter = TRUE
		  AND player_name != $3
		ORDER BY RANDOM()
		LIMIT 5
		""",
		season,
		position,
		answer,
	)
	distractor_pool = [r["player_name"] for r in others]
	if len(distractor_pool) < 3:
		return None
	distractors = random.sample(distractor_pool, 3)
	return {
		"id": f"top_{position}_week_{season}",
		"category": "player",
		"difficulty": 3,
		"question": f"Which {position} scored the most points in a single week during the {season} season?",
		"answer": answer,
		"distractors": distractors,
		"flavor": f"Week {week}, {season} — {answer} ({owner}) {pts:.1f} pts",
	}


async def q_player_owner(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who drafted [player] in year X? (2018+ box score seasons)"""
	box_seasons = [s for s in seasons if s >= 2018]
	if not box_seasons:
		return None
	season = random.choice(box_seasons)
	# pick a notable starter (high scorer)
	row = await db.fetchrow(
		"""
		SELECT b.player_name, t.owner, SUM(b.points_scored) AS total
		FROM box_scores b
		JOIN teams t ON b.team_id = t.team_id
		JOIN matchups m ON b.matchup_id = m.id
		WHERE b.season = $1 AND b.is_starter = TRUE
		  AND NOT m.is_playoffs
		GROUP BY b.player_name, t.owner
		ORDER BY total DESC, b.player_name, t.owner
		OFFSET floor(random() * 10)
		LIMIT 1
		""",
		season,
	)
	if not row:
		return None
	player = row["player_name"]
	answer = row["owner"]
	return {
		"id": f"player_owner_{player.replace(' ', '_')}_{season}",
		"category": "player",
		"difficulty": 3,
		"question": f"Who had {player} on their roster in the {season} season?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{player}, {season} — rostered by {answer}",
	}


# ---------------------------------------------------------------------------
# Category: Head to Head
# ---------------------------------------------------------------------------

async def q_h2h_series_leader(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who leads the all-time H2H series between owner A and owner B?"""
	# pick two owners who have played each other a meaningful number of times
	row = await db.fetchrow(
		"""
		SELECT
			LEAST(ht.owner, at.owner) AS owner_a,
			GREATEST(ht.owner, at.owner) AS owner_b,
			COUNT(*) AS games
		FROM matchups m
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		WHERE ht.owner != at.owner
		GROUP BY LEAST(ht.owner, at.owner), GREATEST(ht.owner, at.owner)
		HAVING COUNT(*) >= 8
		ORDER BY RANDOM()
		LIMIT 1
		""",
	)
	if not row:
		return None
	owner_a = row["owner_a"]
	owner_b = row["owner_b"]

	# get win counts
	wins_a = await db.fetchval(
		"""
		SELECT COUNT(*) FROM matchups m
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		JOIN teams wt ON m.winner_team_id = wt.team_id
		WHERE ((ht.owner = $1 AND at.owner = $2) OR (ht.owner = $2 AND at.owner = $1))
		  AND wt.owner = $1
		""",
		owner_a, owner_b,
	)
	wins_b = await db.fetchval(
		"""
		SELECT COUNT(*) FROM matchups m
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		JOIN teams wt ON m.winner_team_id = wt.team_id
		WHERE ((ht.owner = $1 AND at.owner = $2) OR (ht.owner = $2 AND at.owner = $1))
		  AND wt.owner = $2
		""",
		owner_a, owner_b,
	)
	if wins_a == wins_b:
		return None  # skip ties for cleaner questions
	answer = owner_a if wins_a > wins_b else owner_b
	other = owner_b if answer == owner_a else owner_a
	total = int(row["games"])
	leader_wins = max(wins_a, wins_b)
	trailer_wins = min(wins_a, wins_b)
	return {
		"id": f"h2h_{owner_a.replace(' ', '_')}_{owner_b.replace(' ', '_')}",
		"category": "h2h",
		"difficulty": 3,
		"question": f"Who leads the all-time series between {owner_a} and {owner_b}?",
		"answer": answer,
		"distractors": [other, "Even series", _other_owners(owners, answer, n=1)[0]],
		"flavor": f"{answer} leads {leader_wins}–{trailer_wins} ({total} games played)",
	}


# ---------------------------------------------------------------------------
# Category: All-Time / Career
# ---------------------------------------------------------------------------

async def q_alltime_wins_leader(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who has the most all-time wins?"""
	row = await db.fetchrow(
		"""
		SELECT t.owner, SUM(r.wins) AS total_wins
		FROM records r JOIN teams t ON r.team_id = t.team_id
		GROUP BY t.owner
		ORDER BY total_wins DESC
		LIMIT 1
		""",
	)
	if not row:
		return None
	answer = row["owner"]
	wins = int(row["total_wins"])
	return {
		"id": "alltime_wins_leader",
		"category": "season",
		"difficulty": 1,
		"question": "Who has the most all-time wins in league history?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{answer} — {wins} career wins",
	}


async def q_most_championships(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who has won the most championships?"""
	row = await db.fetchrow(
		"""
		SELECT t.owner, COUNT(*) AS titles
		FROM records r JOIN teams t ON r.team_id = t.team_id
		WHERE r.championship = TRUE
		GROUP BY t.owner
		ORDER BY titles DESC
		LIMIT 1
		""",
	)
	if not row:
		return None
	answer = row["owner"]
	titles = int(row["titles"])
	return {
		"id": "most_championships",
		"category": "season",
		"difficulty": 1,
		"question": "Who has won the most championships in league history?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{answer} — {titles} championships",
	}


async def q_most_sackos(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who has the most sacko finishes?"""
	row = await db.fetchrow(
		"""
		SELECT t.owner, COUNT(*) AS sackos
		FROM records r JOIN teams t ON r.team_id = t.team_id
		WHERE r.sacko = TRUE
		GROUP BY t.owner
		ORDER BY sackos DESC
		LIMIT 1
		""",
	)
	if not row:
		return None
	answer = row["owner"]
	sackos = int(row["sackos"])
	if sackos < 2:
		return None  # not interesting if only 1
	return {
		"id": "most_sackos",
		"category": "season",
		"difficulty": 2,
		"question": "Who has earned the most Sacko finishes in league history?",
		"answer": answer,
		"distractors": _other_owners(owners, answer),
		"flavor": f"{answer} — {sackos} Sacko finishes",
	}


async def q_championship_game_hero(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Who scored the most points in the championship game of year X?"""
	box_seasons = [s for s in seasons if s >= 2018]
	if not box_seasons:
		return None
	season = random.choice(box_seasons)

	champ = await db.fetchrow(
		"""
		SELECT m.id AS matchup_id, m.week,
		       ht.owner AS home_owner, at.owner AS away_owner
		FROM matchups m
		JOIN records r ON r.season = m.season
		              AND r.championship = TRUE
		              AND r.team_id IN (m.home_team_id, m.away_team_id)
		JOIN teams ht ON m.home_team_id = ht.team_id
		JOIN teams at ON m.away_team_id = at.team_id
		WHERE m.season = $1 AND m.is_playoffs = TRUE
		ORDER BY m.week DESC
		LIMIT 1
		""",
		season,
	)
	if not champ:
		return None

	row = await db.fetchrow(
		"""
		SELECT b.player_name, b.points_scored, b.position, t.owner
		FROM box_scores b
		JOIN teams t ON b.team_id = t.team_id
		WHERE b.matchup_id = $1 AND b.is_starter = TRUE AND b.points_scored > 0
		ORDER BY b.points_scored DESC, b.team_id, b.espn_player_id
		LIMIT 1
		""",
		champ["matchup_id"],
	)
	if not row:
		return None

	answer = row["player_name"]
	pts = float(row["points_scored"])
	owner = row["owner"]

	others = await db.fetch(
		"""
		SELECT DISTINCT b.player_name FROM box_scores b
		WHERE b.matchup_id = $1 AND b.is_starter = TRUE
		  AND b.player_name != $2
		ORDER BY RANDOM()
		LIMIT 5
		""",
		champ["matchup_id"],
		answer,
	)
	distractor_pool = [r["player_name"] for r in others]
	if len(distractor_pool) < 3:
		return None
	distractors = random.sample(distractor_pool, 3)

	return {
		"id": f"champ_hero_{season}",
		"category": "player",
		"difficulty": 3,
		"question": f"Which player scored the most points in the {season} championship game?",
		"answer": answer,
		"distractors": distractors,
		"flavor": f"{season} championship — {answer} ({owner}) {pts:.1f} pts, Week {champ['week']}",
	}


async def q_streamed_kicker_dst(db, seasons: list[int], owners: list[str]) -> Optional[dict]:
	"""Which owner started the most unique kickers or DSTs in year X? (streaming indicator)"""
	box_seasons = [s for s in seasons if s >= 2018]
	if not box_seasons:
		return None
	season = random.choice(box_seasons)
	position = random.choice(["K", "D/ST"])
	label = "kickers" if position == "K" else "defenses"

	rows = await db.fetch(
		"""
		SELECT t.owner, COUNT(DISTINCT b.player_name) AS unique_count
		FROM box_scores b
		JOIN teams t ON b.team_id = t.team_id
		JOIN matchups m ON b.matchup_id = m.id
		WHERE b.season = $1 AND b.position = $2 AND b.is_starter = TRUE
		  AND NOT m.is_playoffs
		GROUP BY t.owner
		ORDER BY unique_count DESC, t.owner
		LIMIT 5
		""",
		season,
		position,
	)
	if not rows or len(rows) < 2:
		return None

	top = rows[0]
	if int(top["unique_count"]) < 4:
		return None

	answer = top["owner"]
	count = int(top["unique_count"])
	distractor_pool = [r["owner"] for r in rows[1:]]
	distractors = distractor_pool[:3]
	if len(distractors) < 3:
		distractors += _other_owners(owners, answer, n=3 - len(distractors))

	return {
		"id": f"streamed_{position.replace('/', '')}_{season}",
		"category": "player",
		"difficulty": 3,
		"question": f"Which owner started the most different {label} in the {season} season?",
		"answer": answer,
		"distractors": distractors,
		"flavor": f"{season} — {answer} streamed {count} different {label}",
	}


# ---------------------------------------------------------------------------
# Registry — all generators Claude Code should wire up
# ---------------------------------------------------------------------------

QUESTION_GENERATORS = [
	q_championship_winner,
	q_sacko_winner,
	q_most_points_season,
	q_season_wins_leader,
	q_final_standing,
	q_highest_single_week,
	q_highest_score_exact,
	q_closest_game,
	q_first_overall_pick,
	q_first_overall_player,
	q_draft_position_first_round,
	q_top_scorer_position_week,
	q_player_owner,
	q_h2h_series_leader,
	q_alltime_wins_leader,
	q_most_championships,
	q_most_sackos,
	q_championship_game_hero,
	q_streamed_kicker_dst,
]
