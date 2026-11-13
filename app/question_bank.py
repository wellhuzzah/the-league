"""
question_bank.py: The Tower's questions, built from leaderboards.

Two layers:

  Leaderboards: one async function per metric, (rng, ctx, scope) -> Board or None. A Board holds the
    metric's rows, best first and fully ordered (value, then name), plus the wording for each framing
    the metric supports. Where the site already shows the number, the rows come from the query the page
    uses (career_totals, career_luck, TEAM_WEEKS, draft_tendencies, OPPONENT_RECORDS, the waiver
    records), so the Tower never contradicts a page.

  Framings: most, fewest, pick_of_four and odd_one_out turn any Board into a question.

QUESTION_GENERATORS pairs them (with a scope and a difficulty); the router samples from it.

Rules every question follows:
  - The answer is an owner or an NFL player; a year appears only in the text of landmark questions.
  - Wrong answers are other rows of the same board (the next names down), never random names.
  - No tiebreakers (league rule): a question is skipped when the answer's value equals any option's,
    and "most"/"fewest" are skipped when the answer's value is shared with any other row.
  - Exactly three distinct distractors, none equal to the answer, or no question.
  - All randomness goes through rng; SQL never calls random(), and every query is fully ordered.
  - Fewest, odd-one-out "never" answers, droughts and rivalry subjects need MIN_SEASONS finished seasons.

Data rules: career numbers the Teams and Records pages show come from records (finished seasons); new
leaderboards built from games, box scores, the draft and waivers include the in-progress season. Title,
Sacko and final-standing questions use finished seasons only. Team-game boards leave out toilet bowl
games (TEAM_WEEKS); player box-score boards keep them, as the position explorer, the random player card
and the Team page's best weeks do. Box scores start in 2018, transactions in 2018, outbid claims in 2019.
Playoff boards count WINNERS_BRACKET games only.
"""

import random
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from app.routers.draft import draft_tendencies
from app.routers.players import LATEST_NAME
from app.routers.records import TEAM_WEEKS
from app.routers.seasons import career_luck, season_standings
from app.routers.teams import OPPONENT_RECORDS, career_totals, win_pct
from app.routers.transactions import biggest_bids, most_contested

MIN_SEASONS = 5         # fewest, "never" answers, droughts, rivalry subjects
BIG_WEEK = 140          # 150 tied at the top (josh d and brian h, 8 each); 140 spreads (20, 16, 15, 14)
CLOSE_MARGIN = 2        # "decided by under 2 points"; an equal score isn't decided (CLOSEST_GAMES)
NEMESIS_MIN_GAMES = 8
DRAFTED_BY_MIN = 3      # "who has drafted X the most": X drafted 3+ times by one owner
STARTED_BY_MIN = 30     # "who has started X the most": X started 30+ times for one owner
PLAYER_ROWS = 12        # player boards keep (and name) the top rows plus any tied with the last
PICK4_POOL = 10         # pick of four draws its options from the board's top rows
# No "most keepers" board: keepers began in 2022 and nine owners have kept one every season (5 each),
# so it never has a single leader, and "fewest" would name owners who left before keepers existed.
# No "most FAAB in a season" board: vetoed in review (2026 is the first season with traded FAAB, so the
# only totals over $100 are this season's).
ALL_ROWS = 100000       # "limit" for the waiver records functions: every row

# Team-game scopes (team_games alias tg). Box-score and waiver boards are 2018+ by nature.
SCOPES = {
	"all":       "",
	"since2018": "AND tg.season >= 2018",
	"playoffs":  "AND tg.playoff_tier = 'WINNERS_BRACKET'",
}
SCOPE_SUFFIX = {"all": "", "since2018": " since 2018", "playoffs": " in the playoffs"}
SCOPE_TITLE = {"all": "", "since2018": " (since 2018)", "playoffs": " (playoffs)"}


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
	name: str
	value: Any          # what the board ranks on; equal values are a tie
	shown: str          # the value as the flavor shows it
	seasons: int = 0    # an owner's finished seasons (MIN_SEASONS); 0 for players


@dataclass
class Board:
	key: str                        # stable id: the metric plus any parameter (season, owner, player id)
	category: str                   # shown above the question
	title: str                      # flavor heading
	rows: list                      # best first
	scope: str = "all"
	most: Optional[str] = None      # question text per framing; None = framing not offered
	fewest: Optional[str] = None
	pick4: Optional[str] = None
	odd: Optional[str] = None       # "Which of these has never ...?": value 0 = never
	flavor: Optional[str] = None    # replaces the generated flavor


def ranked(items, desc=True):
	"""(name, value, shown[, seasons]) tuples -> Rows, best value first, then name."""
	return [Row(*i) for i in sorted(items, key=lambda i: (-i[1] if desc else i[1], i[0]))]


def scoped(text, scope):
	"""Question text with the scope phrase before the question mark."""
	return text[:-1] + SCOPE_SUFFIX[scope] + "?" if text else None


class Context:
	"""Per-round cache: each shared query runs at most once per round, whoever asks for it."""

	def __init__(self, db):
		self.db = db
		self._memo = {}

	async def get(self, key, load):
		if key not in self._memo:
			self._memo[key] = await load(self.db)
		return self._memo[key]


def _n(v):
	return str(int(v))


def _pts(v):
	return f"{float(v):.1f}"


def _ordinal(n):
	n = int(n)
	return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _week(season, week):
	return f"{int(season)} W{week}"


def _record(w, l, t):
	return f"{w}-{l}" + (f"-{t}" if t else "")


# ---------------------------------------------------------------------------
# Shared data
# ---------------------------------------------------------------------------

async def _career(ctx):
	return await ctx.get("career", career_totals)


async def _seasons_of(ctx):
	"""owner -> finished seasons (the Teams page's seasons played)."""
	return {c["owner"]: c["seasons_played"] for c in await _career(ctx)}


async def _owner_board_rows(ctx, values, shown=_n, desc=True):
	"""Rows for every owner in league history; values maps owner -> value (missing = 0)."""
	seasons = await _seasons_of(ctx)
	return ranked([(o, values.get(o, 0), shown(values.get(o, 0)), s) for o, s in seasons.items()], desc)


async def _finished_seasons(ctx):
	rows = await ctx.get("finished", lambda db: db.fetch("SELECT DISTINCT season::int AS s FROM records ORDER BY 1"))
	return [r["s"] for r in rows]


async def _current_season(ctx):
	"""The in-progress season (matchups but no records rows), or None between seasons."""
	latest = await ctx.get("latest", lambda db: db.fetchval("SELECT max(season)::int FROM matchups"))
	return latest if latest not in await _finished_seasons(ctx) else None


async def _current_owners(ctx):
	rows = await ctx.get("current_owners", lambda db: db.fetch("""
		SELECT DISTINCT t.owner FROM espn_team_map e JOIN teams t ON t.team_id = e.team_id
		WHERE e.season = (SELECT max(season) FROM espn_team_map) ORDER BY 1
	"""))
	return [r["owner"] for r in rows]


async def _team_weeks(ctx, scope, extra=""):
	"""TEAM_WEEKS (the Records page's team-week rows, toilet bowl aside) in a scope, with extra filters."""
	sql = TEAM_WEEKS + f" {SCOPES[scope]} {extra} ORDER BY tg.season, tg.week, t.owner"
	return await ctx.get(("team_weeks", scope, extra), lambda db: db.fetch(sql))


async def _names(ctx, ids):
	"""espn_player_id -> the player's latest name (the Players page's LATEST_NAME)."""
	ids = sorted(set(ids))
	rows = await ctx.db.fetch(
		f"SELECT p.id, {LATEST_NAME.format(pid='p.id')} AS name FROM unnest($1::int[]) AS p(id) ORDER BY p.id", ids)
	return {r["id"]: r["name"] for r in rows}


async def _player_rows(ctx, counts, shown=_n):
	"""Rows from (espn_player_id, value) pairs, most first: the top PLAYER_ROWS plus any tied with the
	last, named by latest name, then ordered by value and name."""
	counts = sorted(counts, key=lambda c: (-c[1], c[0]))
	if len(counts) > PLAYER_ROWS:
		cutoff = counts[PLAYER_ROWS - 1][1]
		counts = [c for c in counts if c[1] >= cutoff]
	names = await _names(ctx, [pid for pid, _ in counts])
	return ranked([(names[pid], v, shown(v)) for pid, v in counts])


# ---------------------------------------------------------------------------
# Leaderboards: glory and shame
# ---------------------------------------------------------------------------

async def lb_championships(rng, ctx, scope="all"):
	career = await _career(ctx)
	return Board("championships", "glory", "Championships",
		await _owner_board_rows(ctx, {c["owner"]: c["championships"] for c in career}),
		most="Who has won the most championships?",
		pick4="Which of these owners has won the most championships?",
		odd="Which of these owners has never won a championship?")


async def lb_sackos(rng, ctx, scope="all"):
	career = await _career(ctx)
	return Board("sackos", "shame", "Sackos",
		await _owner_board_rows(ctx, {c["owner"]: c["sackos"] for c in career}),
		most="Who has taken the most Sackos?",
		pick4="Which of these owners has taken the most Sackos?",
		odd="Which of these owners has never taken the Sacko?")


async def lb_points_titles(rng, ctx, scope="all"):
	career = await _career(ctx)
	return Board("points_titles", "glory", "Seasons leading the league in points",
		await _owner_board_rows(ctx, {c["owner"]: c["most_points_seasons"] for c in career}),
		most="Who has led the league in points the most seasons?",
		pick4="Which of these owners has led the league in points the most seasons?")


# Playoff appearances (seasons with a winners-bracket game) and winners-bracket wins. No page shows
# these yet; an equal-score game ESPN gave to one side counts as that side's win, as everywhere else.
PLAYOFFS_SQL = """
	SELECT t.owner,
	       COUNT(DISTINCT tg.season) FILTER (WHERE tg.playoff_tier = 'WINNERS_BRACKET')                AS appearances,
	       COUNT(*) FILTER (WHERE tg.playoff_tier = 'WINNERS_BRACKET' AND tg.result = 'W')             AS wins
	FROM teams t
	LEFT JOIN team_games tg ON tg.team_id = t.team_id
	GROUP BY t.owner
	ORDER BY t.owner
"""


async def _playoffs(ctx):
	return await ctx.get("playoffs", lambda db: db.fetch(PLAYOFFS_SQL))


async def lb_playoff_appearances(rng, ctx, scope="all"):
	rows = await _playoffs(ctx)
	return Board("playoff_appearances", "glory", "Playoff appearances",
		await _owner_board_rows(ctx, {r["owner"]: r["appearances"] for r in rows}),
		most="Who has made the playoffs the most times?",
		pick4="Which of these owners has made the playoffs the most times?",
		fewest=f"Among owners with {MIN_SEASONS}+ seasons, who has made the playoffs the fewest times?")


async def lb_playoff_wins(rng, ctx, scope="all"):
	rows = await _playoffs(ctx)
	return Board("playoff_wins", "glory", "Playoff wins",
		await _owner_board_rows(ctx, {r["owner"]: r["wins"] for r in rows}),
		most="Who has the most playoff wins?",
		pick4="Which of these owners has the most playoff wins?",
		fewest=f"Among owners with {MIN_SEASONS}+ seasons, who has the fewest playoff wins?")


async def lb_big_weeks(rng, ctx, scope="all"):
	weeks = await _team_weeks(ctx, scope)
	counts = {}
	for w in weeks:
		if w["score"] >= BIG_WEEK:
			counts[w["owner"]] = counts.get(w["owner"], 0) + 1
	noun = f"{BIG_WEEK}-point " + ("games" if scope == "playoffs" else "weeks")
	return Board(f"big_weeks_{BIG_WEEK}", "glory", f"{noun.capitalize()}{SCOPE_TITLE[scope]}",
		await _owner_board_rows(ctx, counts), scope=scope,
		most=scoped(f"Who has the most {noun}?", scope),
		pick4=scoped(f"Which of these owners has the most {noun}?", scope),
		fewest=scoped(f"Among owners with {MIN_SEASONS}+ seasons, who has the fewest {noun}?", scope))


def _owner_best(weeks, better):
	"""Each owner's best week by better(a, b) -> a beats b; the first one wins a tie (rows come in the
	order the Records page breaks ties)."""
	best = {}
	for w in weeks:
		if w["owner"] not in best or better(w["score"], best[w["owner"]]["score"]):
			best[w["owner"]] = w
	return best


async def lb_high_loss(rng, ctx, scope="all"):
	# Records page "highest_losing": TEAM_WEEKS + result L, ties by season, week
	best = _owner_best(await _team_weeks(ctx, scope, "AND tg.result = 'L'"), lambda a, b: a > b)
	seasons = await _seasons_of(ctx)
	return Board("high_loss", "shame", f"Highest score in a loss{SCOPE_TITLE[scope]}",
		ranked([(o, w["score"], f"{_pts(w['score'])} ({_week(w['season'], w['week'])} vs {w['opponent']})",
		         seasons.get(o, 0)) for o, w in best.items()]),
		scope=scope,
		most=scoped("Who holds the record for the highest score in a loss?", scope),
		pick4=scoped("Which of these owners has the highest-scoring loss?", scope))


async def lb_low_win(rng, ctx, scope="all"):
	# Records page "lowest_winning": TEAM_WEEKS + result W against a team that scored; ties latest first
	weeks = list(reversed(await _team_weeks(ctx, scope, "AND tg.result = 'W' AND tg.opp_score > 0")))
	best = _owner_best(weeks, lambda a, b: a < b)
	seasons = await _seasons_of(ctx)
	return Board("low_win", "luck", f"Lowest score in a win{SCOPE_TITLE[scope]}",
		ranked([(o, w["score"], f"{_pts(w['score'])} ({_week(w['season'], w['week'])} vs {w['opponent']})",
		         seasons.get(o, 0)) for o, w in best.items()], desc=False),
		scope=scope,
		most=scoped("Who holds the record for the lowest score in a win?", scope),
		pick4=scoped("Which of these owners has the lowest-scoring win?", scope))


async def lb_title_drought(rng, ctx, scope="all"):
	"""Current owners with MIN_SEASONS+ finished seasons: finished seasons played since their last
	title (all of them if they never won)."""
	rows = await ctx.get("titles_by_season", lambda db: db.fetch("""
		SELECT t.owner, r.season::int AS season, r.championship
		FROM records r JOIN teams t ON t.team_id = r.team_id
		ORDER BY t.owner, r.season
	"""))
	current = set(await _current_owners(ctx))
	history = {}
	for r in rows:
		history.setdefault(r["owner"], []).append(r)
	items = []
	for owner, played in history.items():
		if owner not in current or len(played) < MIN_SEASONS:
			continue
		titles = [r["season"] for r in played if r["championship"]]
		drought = sum(1 for r in played if not titles or r["season"] > titles[-1])
		items.append((owner, drought, f"{drought} seasons ({f'last title {titles[-1]}' if titles else 'never won'})",
		              len(played)))
	return Board("title_drought", "shame", "Seasons since a title (current owners)", ranked(items),
		most="Among current owners, who has gone the most seasons without a championship?",
		pick4="Which of these current owners has gone the most seasons without a championship?")


# ---------------------------------------------------------------------------
# Leaderboards: close games and luck
# ---------------------------------------------------------------------------

# CLOSEST_GAMES' definition per team: decided games (a winner, unequal scores), toilet bowl aside
CLOSE = f"AND tg.result IN ('W', 'L') AND tg.score <> tg.opp_score AND abs(tg.score - tg.opp_score) < {CLOSE_MARGIN}"


async def lb_close_games(rng, ctx, scope="all"):
	counts = {}
	for w in await _team_weeks(ctx, scope, CLOSE):
		counts[w["owner"]] = counts.get(w["owner"], 0) + 1
	return Board("close_games", "close games", f"Games decided by under {CLOSE_MARGIN} points{SCOPE_TITLE[scope]}",
		await _owner_board_rows(ctx, counts), scope=scope,
		most=scoped(f"Who has played the most games decided by under {CLOSE_MARGIN} points?", scope),
		pick4=scoped(f"Which of these owners has played the most games decided by under {CLOSE_MARGIN} points?", scope),
		fewest=scoped(f"Among owners with {MIN_SEASONS}+ seasons, who has played the fewest games decided by "
		              f"under {CLOSE_MARGIN} points?", scope))


async def lb_close_losses(rng, ctx, scope="all"):
	counts = {}
	for w in await _team_weeks(ctx, scope, CLOSE):
		if w["score"] < w["opp_score"]:
			counts[w["owner"]] = counts.get(w["owner"], 0) + 1
	return Board("close_losses", "close games", f"Losses by under {CLOSE_MARGIN} points{SCOPE_TITLE[scope]}",
		await _owner_board_rows(ctx, counts), scope=scope,
		most=scoped(f"Who has lost the most games by under {CLOSE_MARGIN} points?", scope),
		pick4=scoped(f"Which of these owners has lost the most games by under {CLOSE_MARGIN} points?", scope))


async def _luck_rows(ctx, desc):
	luck = await ctx.get("luck", career_luck)   # /records/luck: every season, the in-progress one included
	seasons = await _seasons_of(ctx)
	return ranked([(r["owner"], r["luck"], f"{float(r['luck']):+.2f}", seasons.get(r["owner"], 0)) for r in luck], desc)


async def lb_luckiest(rng, ctx, scope="all"):
	return Board("luck_high", "luck", "Career Bullshit Index", await _luck_rows(ctx, True),
		most="Who has the highest career Bullshit Index (the luckiest owner)?",
		pick4="Which of these owners has the highest career Bullshit Index?")


async def lb_unluckiest(rng, ctx, scope="all"):
	return Board("luck_low", "luck", "Career Bullshit Index, lowest first", await _luck_rows(ctx, False),
		most="Who has the lowest career Bullshit Index (the unluckiest owner)?",
		pick4="Which of these owners has the lowest career Bullshit Index?")


async def lb_points_against(rng, ctx, scope="all"):
	career = await _career(ctx)
	finished = await _finished_seasons(ctx)
	return Board("points_against", "luck", f"Career points against (through {finished[-1]})",
		await _owner_board_rows(ctx, {c["owner"]: c["total_points_against"] for c in career}, _pts),
		most="Who has had the most points scored against them?",
		pick4="Which of these owners has had the most points scored against them?")


# ---------------------------------------------------------------------------
# Leaderboards: rivalries (an owner picked with rng)
# ---------------------------------------------------------------------------

async def _rival(rng, ctx):
	"""A MIN_SEASONS+ owner, picked with rng, and their rows of OPPONENT_RECORDS (the Team page's
	record against every opponent, toilet bowl aside)."""
	pairs = await ctx.get("opponents", lambda db: db.fetch(OPPONENT_RECORDS.format(where="")))
	seasons = await _seasons_of(ctx)
	owner = rng.choice(sorted(o for o, s in seasons.items() if s >= MIN_SEASONS))
	return owner, [p for p in pairs if p["owner"] == owner], seasons


async def lb_beat_owner(rng, ctx, scope="all"):
	owner, rows, seasons = await _rival(rng, ctx)
	return Board(f"beat_owner:{owner}", "rivalry", f"Wins against {owner}",
		ranked([(r["opponent"], r["losses"], _record(r["losses"], r["wins"], r["ties"]), seasons.get(r["opponent"], 0))
		        for r in rows]),
		most=f"Who has beaten {owner} the most times?")


async def lb_owner_beat(rng, ctx, scope="all"):
	owner, rows, seasons = await _rival(rng, ctx)
	return Board(f"owner_beat:{owner}", "rivalry", f"{owner}'s wins by opponent",
		ranked([(r["opponent"], r["wins"], _record(r["wins"], r["losses"], r["ties"]), seasons.get(r["opponent"], 0))
		        for r in rows]),
		most=f"Who has {owner} beaten the most times?")


async def lb_nemesis(rng, ctx, scope="all"):
	owner, rows, seasons = await _rival(rng, ctx)
	items = []
	for r in rows:
		if r["total_games"] >= NEMESIS_MIN_GAMES:
			pct = win_pct(r["wins"], r["losses"], r["ties"])
			items.append((r["opponent"], pct, f"{_record(r['wins'], r['losses'], r['ties'])} "
			              f"({f'{pct:.3f}'.lstrip('0')})", seasons.get(r["opponent"], 0)))
	return Board(f"nemesis:{owner}", "rivalry", f"{owner}'s worst records ({NEMESIS_MIN_GAMES}+ games)",
		ranked(items, desc=False),
		most=f"Who is {owner}'s nemesis, the opponent {owner} has the worst record against "
		     f"({NEMESIS_MIN_GAMES}+ games)?")


# ---------------------------------------------------------------------------
# Leaderboards: draft (every draft, the current one included)
# ---------------------------------------------------------------------------

DRAFT_COUNTS_SQL = """
	SELECT t.owner,
	       COUNT(d.id) FILTER (WHERE d.overall_pick = 1) AS first_picks
	FROM teams t
	LEFT JOIN draft_picks d ON d.team_id = t.team_id
	GROUP BY t.owner
	ORDER BY t.owner
"""


async def lb_first_overall_picks(rng, ctx, scope="all"):
	rows = await ctx.get("draft_counts", lambda db: db.fetch(DRAFT_COUNTS_SQL))
	return Board("first_overall_picks", "draft", "#1 overall picks",
		await _owner_board_rows(ctx, {r["owner"]: r["first_picks"] for r in rows}),
		most="Who has held the #1 overall pick the most times?",
		pick4="Which of these owners has held the #1 overall pick the most times?",
		odd="Which of these owners has never held the #1 overall pick?")


async def lb_first_round_qbs(rng, ctx, scope="all"):
	tendencies = await ctx.get("tendencies", draft_tendencies)   # the Records page's draft tendencies
	return Board("first_round_qbs", "draft", "First-round quarterbacks",
		await _owner_board_rows(ctx, {t["owner"]: t["rounds"][0]["counts"]["QB"] for t in tendencies}),
		most="Who has taken the most quarterbacks in the first round?",
		pick4="Which of these owners has taken the most quarterbacks in the first round?")


async def _earliest(ctx, position, key, title, most, pick4):
	picks = await ctx.db.fetch("""
		SELECT d.espn_player_id, d.player_name, d.overall_pick, d.season::int AS season, t.owner
		FROM draft_picks d JOIN teams t ON t.team_id = d.team_id
		WHERE d.position = $1
		ORDER BY d.overall_pick, d.season, d.espn_player_id
		LIMIT 200
	""", position)
	seen, items = set(), []
	for p in picks:   # each player's earliest pick; the name as drafted (the Draft page's board)
		if p["espn_player_id"] not in seen:
			seen.add(p["espn_player_id"])
			items.append((p["player_name"], p["overall_pick"], f"pick {p['overall_pick']} ({p['season']}, {p['owner']})"))
	return Board(key, "draft", title, ranked(items, desc=False)[:PLAYER_ROWS], most=most, pick4=pick4)


async def lb_earliest_kicker(rng, ctx, scope="all"):
	return await _earliest(ctx, "K", "earliest_kicker", "Earliest kickers drafted",
		"Which kicker has gone earliest in a league draft?",
		"Which of these kickers went earliest in a league draft?")


async def lb_earliest_defense(rng, ctx, scope="all"):
	return await _earliest(ctx, "D/ST", "earliest_defense", "Earliest defenses drafted",
		"Which defense has gone earliest in a league draft?",
		"Which of these defenses went earliest in a league draft?")


async def lb_first_round_player(rng, ctx, scope="all"):
	counts = await ctx.db.fetch("""
		SELECT espn_player_id, COUNT(*) AS n FROM draft_picks WHERE round_num = 1
		GROUP BY espn_player_id ORDER BY n DESC, espn_player_id
	""")
	return Board("first_round_player", "players", "First-round selections",
		await _player_rows(ctx, [(r["espn_player_id"], r["n"]) for r in counts]),
		most="Which player has been drafted in the first round the most times?",
		pick4="Which of these players has been drafted in the first round the most times?")


async def lb_kept_player(rng, ctx, scope="all"):
	counts = await ctx.db.fetch("""
		SELECT espn_player_id, COUNT(*) AS n FROM draft_picks WHERE is_keeper
		GROUP BY espn_player_id ORDER BY n DESC, espn_player_id
	""")
	return Board("kept_player", "players", "Times kept",
		await _player_rows(ctx, [(r["espn_player_id"], r["n"]) for r in counts]),
		most="Which player has been kept the most times?")


async def _owners_of_player(rng, ctx, cache_key, sql, minimum):
	"""Pick (with rng) a player some owner has `minimum`+ of, with a clear leader and 4+ owners; return
	the player's id, latest name and their owner -> count rows."""
	rows = await ctx.get(cache_key, lambda db: db.fetch(sql, minimum))
	by_player = {}
	for r in rows:
		by_player.setdefault(r["espn_player_id"], []).append((r["owner"], r["n"]))
	eligible = sorted(pid for pid, owners in by_player.items()
	                  if len(owners) >= 4 and owners[0][1] >= minimum and owners[0][1] > owners[1][1])
	if not eligible:
		return None
	pid = rng.choice(eligible)
	name = (await _names(ctx, [pid]))[pid]
	seasons = await _seasons_of(ctx)
	return pid, name, ranked([(o, n, _n(n), seasons.get(o, 0)) for o, n in by_player[pid]])


async def lb_drafted_by(rng, ctx, scope="all"):
	picked = await _owners_of_player(rng, ctx, "drafted_by", """
		SELECT d.espn_player_id, t.owner, COUNT(*) AS n
		FROM draft_picks d JOIN teams t ON t.team_id = d.team_id
		WHERE d.espn_player_id IN (SELECT espn_player_id FROM draft_picks
		                           GROUP BY espn_player_id, team_id HAVING COUNT(*) >= $1)
		GROUP BY d.espn_player_id, t.owner
		ORDER BY d.espn_player_id, n DESC, t.owner
	""", DRAFTED_BY_MIN)
	if not picked:
		return None
	pid, name, rows = picked
	return Board(f"drafted_by:{pid}", "players", f"Times drafting {name}", rows,
		most=f"Who has drafted {name} the most times?")


# ---------------------------------------------------------------------------
# Leaderboards: box scores (2018+; toilet bowl games kept, as on the player pages)
# ---------------------------------------------------------------------------

async def lb_started_by(rng, ctx, scope="since2018"):
	# the random player card's "top owner" count: starts, every game
	picked = await _owners_of_player(rng, ctx, "started_by", """
		SELECT bs.espn_player_id, t.owner, COUNT(*) AS n
		FROM box_scores bs JOIN teams t ON t.team_id = bs.team_id
		WHERE bs.is_starter
		  AND bs.espn_player_id IN (SELECT espn_player_id FROM box_scores WHERE is_starter
		                            GROUP BY espn_player_id, team_id HAVING COUNT(*) >= $1)
		GROUP BY bs.espn_player_id, t.owner
		ORDER BY bs.espn_player_id, n DESC, t.owner
	""", STARTED_BY_MIN)
	if not picked:
		return None
	pid, name, rows = picked
	return Board(f"started_by:{pid}", "players", f"Starts of {name} (since 2018)", rows, scope="since2018",
		most=f"Who has started {name} the most times since 2018?")


async def _best_player_weeks(ctx):
	"""Starter weeks, best first, in the position explorer's order; with latest names."""
	async def load(db):
		weeks = await db.fetch("""
			SELECT bs.espn_player_id, bs.points_scored, bs.season::int AS season, bs.week, t.owner
			FROM box_scores bs JOIN teams t ON t.team_id = bs.team_id
			WHERE bs.is_starter
			ORDER BY bs.points_scored DESC, bs.season, bs.week, bs.team_id, bs.espn_player_id
			LIMIT 2000
		""")
		return weeks
	return await ctx.get("player_weeks", load)


async def lb_best_player_week(rng, ctx, scope="since2018"):
	seen, best = set(), []
	for w in await _best_player_weeks(ctx):
		if w["espn_player_id"] not in seen:
			seen.add(w["espn_player_id"])
			best.append(w)
	best = best[:PLAYER_ROWS]
	names = await _names(ctx, [w["espn_player_id"] for w in best])
	return Board("best_player_week", "box scores", "Best single-player weeks (since 2018)",
		ranked([(names[w["espn_player_id"]], w["points_scored"],
		         f"{_pts(w['points_scored'])} ({w['owner']}, {_week(w['season'], w['week'])})") for w in best]),
		scope="since2018",
		most="Which player has the highest single-week score for a league team since 2018?",
		pick4="Which of these players has the highest single-week score for a league team since 2018?")


async def lb_best_player_week_owner(rng, ctx, scope="since2018"):
	best = {}
	for w in await _best_player_weeks(ctx):
		best.setdefault(w["owner"], w)
	names = await _names(ctx, [w["espn_player_id"] for w in best.values()])
	seasons = await _seasons_of(ctx)
	return Board("best_player_week_owner", "box scores", "Best single-player week started (since 2018)",
		ranked([(o, w["points_scored"], f"{names[w['espn_player_id']]} {_pts(w['points_scored'])} "
		         f"({_week(w['season'], w['week'])})", seasons.get(o, 0)) for o, w in best.items()]),
		scope="since2018",
		most="Who started the highest-scoring single-player week since 2018?",
		pick4="Which of these owners has started the highest-scoring single-player week since 2018?")


# ---------------------------------------------------------------------------
# Leaderboards: waivers (transactions 2018+, outbid claims 2019+)
# ---------------------------------------------------------------------------

async def _bids(ctx):
	return await ctx.get("bids", lambda db: biggest_bids(db, ALL_ROWS))   # /transactions/records/biggest-bids


async def lb_biggest_bid_player(rng, ctx, scope="since2018"):
	best = {}
	for b in await _bids(ctx):   # bid order, so the first row per player is their biggest
		best.setdefault(b["player_name"], b)
	return Board("biggest_bid_player", "waivers", "Biggest winning waiver bids (since 2018)",
		ranked([(p, b["bid_amount"], f"${b['bid_amount']} ({b['owner']}, {int(b['season'])})")
		        for p, b in best.items()])[:PLAYER_ROWS],
		scope="since2018",
		most="Which player drew the biggest winning waiver bid since 2018?",
		pick4="Which of these players drew the biggest winning waiver bid since 2018?")


async def lb_biggest_bid_owner(rng, ctx, scope="since2018"):
	best = {}
	for b in await _bids(ctx):
		best.setdefault(b["owner"], b)
	seasons = await _seasons_of(ctx)
	return Board("biggest_bid_owner", "waivers", "Biggest winning waiver bid (since 2018)",
		ranked([(o, b["bid_amount"], f"${b['bid_amount']} on {b['player_name']} ({int(b['season'])})",
		         seasons.get(o, 0)) for o, b in best.items()]),
		scope="since2018",
		most="Who has made the biggest winning waiver bid since 2018?",
		pick4="Which of these owners has made the biggest winning waiver bid since 2018?")


async def lb_most_contested(rng, ctx, scope="since2019"):
	best = {}
	for r in await most_contested(ctx.db, ALL_ROWS):   # /transactions/records/most-contested
		best.setdefault(r["espn_player_id"], r)
	return Board("most_contested", "waivers", "Most-contested waiver pickups (since 2019)",
		ranked([(r["player_name"], r["teams"], f"{r['teams']} teams ({_week(r['season'], r['week'])})")
		        for r in best.values()])[:PLAYER_ROWS],
		scope="since2019",
		most="Which waiver pickup drew claims from the most teams in one run since 2019?",
		pick4="Which of these waiver pickups drew claims from the most teams in one run since 2019?")


# ---------------------------------------------------------------------------
# Leaderboards: one season (picked with rng)
# ---------------------------------------------------------------------------

async def _standings(ctx, season):
	return await ctx.get(("standings", season), lambda db: season_standings(db, season))   # the Season page


async def _standing_rows(ctx, season, desc):
	seasons = await _seasons_of(ctx)
	return ranked([(r["owner"], r["final_standing"], _ordinal(r["final_standing"]), seasons.get(r["owner"], 0))
	               for r in await _standings(ctx, season)], desc)


async def lb_champion(rng, ctx, scope="all"):
	season = rng.choice(await _finished_seasons(ctx))
	return Board(f"champion:{season}", "season", f"{season} final standings",
		await _standing_rows(ctx, season, desc=False),
		most=f"Who won the {season} championship?")


async def lb_sacko(rng, ctx, scope="all"):
	season = rng.choice(await _finished_seasons(ctx))
	return Board(f"sacko:{season}", "season", f"{season} final standings",
		await _standing_rows(ctx, season, desc=True),
		most=f"Who took the Sacko in {season}?")


async def lb_season_points(rng, ctx, scope="all"):
	current = await _current_season(ctx)
	season = rng.choice(await _finished_seasons(ctx) + ([current] if current else []))
	standings = await _standings(ctx, season)
	if not standings:
		return None
	seasons = await _seasons_of(ctx)
	rows = ranked([(r["owner"], r["points_for"], _pts(r["points_for"]), seasons.get(r["owner"], 0)) for r in standings])
	if season == current:
		return Board(f"season_points:{season}", "season",
			f"{season} points so far (through week {standings[0]['through_week']})", rows,
			most=f"Who has scored the most points so far in {season}?")
	return Board(f"season_points:{season}", "season", f"{season} regular-season points", rows,
		most=f"Who scored the most points in the {season} regular season?")


async def lb_first_overall(rng, ctx, scope="all"):
	seasons = await ctx.get("draft_seasons",
		lambda db: db.fetch("SELECT DISTINCT season::int AS s FROM draft_picks ORDER BY 1"))
	season = rng.choice([r["s"] for r in seasons])
	picks = await ctx.db.fetch("""
		SELECT d.overall_pick, d.player_name, t.owner
		FROM draft_picks d JOIN teams t ON t.team_id = d.team_id
		WHERE d.season = $1 AND d.round_num = 1
		ORDER BY d.overall_pick
	""", season)
	return Board(f"first_overall:{season}", "draft", f"{season} first round",
		ranked([(p["player_name"], p["overall_pick"], f"#{p['overall_pick']} ({p['owner']})") for p in picks],
		       desc=False),
		most=f"Who was the #1 overall pick in the {season} draft?")


async def lb_champion_roster(rng, ctx, scope="since2018"):
	"""Who had {player} on their {season} championship team: a top-5 starter of a 2018+ champion who
	played for none of the 2nd-4th place teams that season (they're the wrong answers)."""
	season = rng.choice([s for s in await _finished_seasons(ctx) if s >= 2018])
	standings = await _standings(ctx, season)
	champ = next(r for r in standings if r["final_standing"] == 1)
	others = [r["team_id"] for r in standings if 2 <= r["final_standing"] <= 4]
	starters = await ctx.db.fetch("""
		SELECT bs.espn_player_id, COUNT(*) AS starts, SUM(bs.points_scored) AS pts
		FROM box_scores bs
		WHERE bs.season = $1 AND bs.team_id = $2 AND bs.is_starter
		  AND bs.espn_player_id NOT IN (SELECT espn_player_id FROM box_scores
		                                WHERE season = $1 AND team_id = ANY($3::int[]))
		GROUP BY bs.espn_player_id
		ORDER BY pts DESC, bs.espn_player_id
		LIMIT 5
	""", season, champ["team_id"], others)
	if not starters:
		return None
	pick = rng.choice(starters)
	name = (await _names(ctx, [pick["espn_player_id"]]))[pick["espn_player_id"]]
	return Board(f"champion_roster:{season}:{pick['espn_player_id']}", "season", f"{season} final standings",
		await _standing_rows(ctx, season, desc=False), scope="since2018",
		most=f"Who had {name} on their {season} championship team?",
		flavor=f"{champ['owner']} won {season}; {name} scored {_pts(pick['pts'])} in {pick['starts']} starts for them")


# ---------------------------------------------------------------------------
# Framings
# ---------------------------------------------------------------------------

def _flavor(title, rows):
	return f"{title}: " + " · ".join(f"{r.name} {r.shown}" for r in rows)


def _least_plausible(answer, distractors):
	"""Two of the three distractors, the order the client removes them: furthest from the answer's
	value first; on equal distance, the one further down the board."""
	order = sorted(range(3), key=lambda i: (-abs(float(distractors[i].value) - float(answer.value)), -i))
	return [distractors[i] for i in order[:2]]


def _question(board, framing, text, answer, distractors, eliminate, difficulty, flavor, option_ids=False):
	names = [answer.name] + [d.name for d in distractors]
	if len(distractors) != 3 or len(set(names)) != 4 or any(d.value == answer.value for d in distractors):
		return None
	return {
		"id": "|".join([board.key, framing, board.scope] + (sorted(names) if option_ids else [])),
		"framing": framing,
		"category": board.category,
		"difficulty": difficulty,
		"question": text,
		"answer": answer.name,
		"distractors": [d.name for d in distractors],
		"eliminate": [d.name for d in eliminate],
		"flavor": board.flavor or flavor,
		"values": {r.name: r.value for r in [answer, *distractors]},   # dump/tests only; the API drops it
	}


def most(board, rng, difficulty):
	"""The board's top row; wrong answers are ranks 2-4. Skipped when the top is tied."""
	rows = board.rows
	if not board.most or len(rows) < 4 or rows[0].value == rows[1].value:
		return None
	answer, distractors = rows[0], rows[1:4]
	return _question(board, "most", board.most, answer, distractors, _least_plausible(answer, distractors),
	                 difficulty, _flavor(board.title, rows[:3]))


def fewest(board, rng, difficulty):
	"""The bottom row among MIN_SEASONS+ owners; wrong answers are the next three up."""
	bottom = [r for r in reversed(board.rows) if r.seasons >= MIN_SEASONS]
	if not board.fewest or len(bottom) < 4 or bottom[0].value == bottom[1].value:
		return None
	answer, distractors = bottom[0], bottom[1:4]
	return _question(board, "fewest", board.fewest, answer, distractors, _least_plausible(answer, distractors),
	                 difficulty, _flavor(f"{board.title}, fewest ({MIN_SEASONS}+ seasons)", bottom[:3]))


def pick_of_four(board, rng, difficulty):
	"""Four of the board's top PICK4_POOL rows with no tie at the top among them; the best one is right."""
	pool = board.rows[:PICK4_POOL]
	if not board.pick4 or len(pool) < 4:
		return None
	for _ in range(8):
		four = [pool[i] for i in sorted(rng.sample(range(len(pool)), 4))]
		if four[0].value != four[1].value:
			break
	else:
		return None
	answer, distractors = four[0], four[1:]
	return _question(board, "pick4", board.pick4, answer, distractors, _least_plausible(answer, distractors),
	                 difficulty, _flavor(board.title, four), option_ids=True)


def odd_one_out(board, rng, difficulty):
	"""One MIN_SEASONS+ owner at 0 (never) among three above 0; removal order is random."""
	nevers = [r for r in board.rows if r.value == 0 and r.seasons >= MIN_SEASONS]
	haves = [r for r in board.rows if r.value > 0]
	if not board.odd or not nevers or len(haves) < 3:
		return None
	answer = rng.choice(nevers)
	distractors = [haves[i] for i in sorted(rng.sample(range(len(haves)), 3))]
	return _question(board, "odd", board.odd, answer, distractors, rng.sample(distractors, 2),
	                 difficulty, _flavor(board.title, distractors + [answer]), option_ids=True)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Generator:
	"""A leaderboard, a framing, a scope and the difficulty that combination plays at."""
	board: Callable
	framing: Callable
	difficulty: int
	scope: str = "all"

	@property
	def base(self):
		"""The leaderboard; a round holds at most two questions from one."""
		return self.board.__name__.removeprefix("lb_")

	@property
	def name(self):
		return f"{self.base}/{self.framing.__name__}/{self.scope}"

	async def __call__(self, rng: random.Random, ctx: Context) -> Optional[dict]:
		board = await self.board(rng, ctx, scope=self.scope)
		if board is None:
			return None
		question = self.framing(replace(board, scope=self.scope), rng, self.difficulty)
		return question and {**question, "board": self.base}


def _registry():
	gens = []

	def add(board, framing, difficulty, *scopes):
		gens.extend(Generator(board, framing, difficulty, s) for s in scopes or ("all",))

	# Difficulty 1: all-time "most"
	for b in (lb_championships, lb_sackos, lb_playoff_appearances, lb_playoff_wins, lb_points_titles,
	          lb_big_weeks, lb_high_loss, lb_low_win, lb_title_drought, lb_close_games, lb_close_losses,
	          lb_luckiest, lb_unluckiest, lb_points_against, lb_first_overall_picks, lb_first_round_qbs,
	          lb_earliest_kicker, lb_earliest_defense, lb_first_round_player, lb_kept_player):
		add(b, most, 1)

	# Difficulty 2: scoped leaderboards, fewest, pick of four
	add(lb_big_weeks, most, 2, "since2018", "playoffs")
	add(lb_high_loss, most, 2, "playoffs")
	add(lb_low_win, most, 2, "playoffs")
	add(lb_close_games, most, 2, "since2018")
	for b in (lb_best_player_week, lb_best_player_week_owner, lb_biggest_bid_player, lb_biggest_bid_owner):
		add(b, most, 2, "since2018")
	add(lb_most_contested, most, 2, "since2019")
	for b in (lb_playoff_appearances, lb_playoff_wins, lb_big_weeks, lb_close_games):
		add(b, fewest, 2)
	for b in (lb_championships, lb_sackos, lb_playoff_appearances, lb_playoff_wins, lb_points_titles,
	          lb_big_weeks, lb_high_loss, lb_low_win, lb_title_drought, lb_close_games, lb_close_losses,
	          lb_luckiest, lb_unluckiest, lb_points_against, lb_first_overall_picks, lb_first_round_qbs,
	          lb_earliest_kicker, lb_earliest_defense, lb_first_round_player):
		add(b, pick_of_four, 2)
	for b in (lb_best_player_week, lb_best_player_week_owner, lb_biggest_bid_player, lb_biggest_bid_owner):
		add(b, pick_of_four, 2, "since2018")
	add(lb_most_contested, pick_of_four, 2, "since2019")

	# Difficulty 3: one season, odd one out, rivalries, one player
	for b in (lb_champion, lb_sacko, lb_season_points, lb_first_overall, lb_beat_owner, lb_owner_beat,
	          lb_nemesis, lb_drafted_by):
		add(b, most, 3)
	add(lb_champion_roster, most, 3, "since2018")
	add(lb_started_by, most, 3, "since2018")
	for b in (lb_championships, lb_sackos, lb_first_overall_picks):
		add(b, odd_one_out, 3)
	return gens


QUESTION_GENERATORS = _registry()
