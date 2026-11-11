from fastapi import APIRouter
from app.database import concurrently
from app.params import top_n, without
from app.routers.draft import draft_tendencies
from app.routers.seasons import career_luck
from app.routers.teams import career_totals

router = APIRouter(
    prefix="/records",
    tags=["records"]
)

# Each list is the top 10 by its value, plus every row tied with the 10th (league rule: equal
# values share a rank), so a list can be longer than 10.
LIST_SIZE = 10

# One team's side of each game (team_games), with both owners. Within a tie, rows are ordered by
# date (direction chosen per list to match the order the old queries happened to return), then owner.
# adjustment/opp_adjustment: a league score adjustment included in the score (0 almost always).
# Toilet bowl games (the losers' ladder) are left out of every single-game list here: lineups there
# often go unset, which skews low scores, blowouts and the rest. Callers add conditions with AND.
TEAM_WEEKS = """
    SELECT tg.season, tg.week, tg.is_playoffs, tg.playoff_tier, t.owner, t.team_id, tg.score, tg.opp_score,
           tg.adjustment, tg.opp_adjustment, o.owner AS opponent
    FROM team_games tg
    JOIN teams t ON t.team_id = tg.team_id
    JOIN teams o ON o.team_id = tg.opp_id
    WHERE tg.playoff_tier IS DISTINCT FROM 'LOSERS_CONSOLATION_LADDER'
"""

SEASON_TOTALS = """
    SELECT
        t.owner,
        t.team_id,
        r.season,
        r.points_for,
        r.points_against,
        r.wins,
        r.losses,
        r.final_standing
    FROM records r
    JOIN teams t ON r.team_id = t.team_id
"""

# Winning and losing streaks in one pass. A tie game ends a streak (it takes a row number but
# belongs to neither group). Toilet bowl games (the losers' ladder) are left out, like byes: teams
# often don't set lineups for them, so they neither extend nor end a streak. The start and end are the streak's first and last games in
# (season, week) order, so a streak spanning seasons ends in the later season's week, not at the
# largest week number.
STREAKS = """
    WITH ordered_games AS (
        SELECT tg.team_id, t.owner, tg.season, tg.week,
               CASE tg.result WHEN 'W' THEN 1 WHEN 'L' THEN 0 END              AS won,
               ROW_NUMBER() OVER (PARTITION BY tg.team_id ORDER BY tg.season, tg.week) AS rn
        FROM team_games tg
        JOIN teams t ON t.team_id = tg.team_id
        WHERE tg.playoff_tier IS DISTINCT FROM 'LOSERS_CONSOLATION_LADDER'
    ),
    grouped AS (
        SELECT team_id, owner, season, week, won, rn,
               rn - ROW_NUMBER() OVER (PARTITION BY team_id, won ORDER BY rn)  AS grp
        FROM ordered_games
        WHERE won IS NOT NULL
    ),
    streaks AS (
        SELECT team_id, owner, won,
               COUNT(*)                              AS streak_length,
               MIN(season)                           AS start_season,
               (ARRAY_AGG(week ORDER BY rn))[1]      AS start_week,
               MAX(season)                           AS end_season,
               (ARRAY_AGG(week ORDER BY rn DESC))[1] AS end_week
        FROM grouped
        GROUP BY team_id, owner, won, grp
    ),
    ranked AS (
        SELECT *, RANK() OVER (PARTITION BY won ORDER BY streak_length DESC) AS place
        FROM streaks
    )
    SELECT won, owner, team_id, streak_length, start_season, start_week, end_season, end_week
    FROM ranked
    WHERE place <= $1
    ORDER BY won DESC, streak_length DESC, start_season, start_week, owner
"""

MARGINS = """
    SELECT
        m.season,
        m.week,
        m.is_playoffs,
        m.playoff_tier,
        m.home_score,
        m.away_score,
        m.home_adjustment,
        m.away_adjustment,
        m.margin,
        t_home.owner AS home_owner,
        t_away.owner AS away_owner,
        t_winner.owner AS winner
    FROM matchups m
    JOIN teams t_home   ON m.home_team_id   = t_home.team_id
    JOIN teams t_away   ON m.away_team_id   = t_away.team_id
    LEFT JOIN teams t_winner ON m.winner_team_id = t_winner.team_id
    WHERE m.winner_team_id IS NOT NULL
      AND m.playoff_tier IS DISTINCT FROM 'LOSERS_CONSOLATION_LADDER'
      {extra}
"""

# Closest games: an equal score is a tie even when ESPN's tiebreak named a winner, so it's left out.
CLOSEST_GAMES = MARGINS.format(extra="AND m.home_score <> m.away_score")


def ranked(query, value, direction, tie_break):
    """top_n of LIST_SIZE by value; within a tie, tie_break (output column names)."""
    return top_n(query, f"{value} {direction}", f"{value} {direction}, {tie_break}", "$1")


def scores(rows):
    return [{**dict(r), "score": float(r["score"]), "opp_score": float(r["opp_score"]),
             "adjustment": float(r["adjustment"]), "opp_adjustment": float(r["opp_adjustment"])} for r in rows]


def season_points(rows):
    return [{**dict(r), "points_for": float(r["points_for"]), "points_against": float(r["points_against"])}
            for r in rows]


def margins(rows):
    return [{**dict(r), "margin": float(r["margin"]), "home_score": float(r["home_score"]),
             "away_score": float(r["away_score"]), "home_adjustment": float(r["home_adjustment"]),
             "away_adjustment": float(r["away_adjustment"])} for r in rows]


@router.get("/summary")
async def get_records_summary():
    """Every list on the Records page in one call (it used to make 12)."""
    (careers, high_season, low_season, best_week, worst_week, streaks, losing, winning,
     blowouts, closest, tendencies) = await concurrently(
        career_totals,
        lambda db: db.fetch(ranked(SEASON_TOTALS, "points_for", "DESC", "season, owner"), LIST_SIZE),
        lambda db: db.fetch(ranked(SEASON_TOTALS, "points_for", "ASC", "season, owner"), LIST_SIZE),
        lambda db: db.fetch(ranked(TEAM_WEEKS, "score", "DESC", "season, week, owner"), LIST_SIZE),
        lambda db: db.fetch(ranked(TEAM_WEEKS, "score", "ASC", "season, week, owner"), LIST_SIZE),
        lambda db: db.fetch(STREAKS, LIST_SIZE),
        # Highest scores that still lost: the unluckiest weeks
        lambda db: db.fetch(ranked(TEAM_WEEKS + "AND tg.result = 'L'", "score", "DESC", "season, week, owner"), LIST_SIZE),
        # Lowest scores that still won (against a team that scored): the luckiest weeks
        lambda db: db.fetch(ranked(TEAM_WEEKS + "AND tg.result = 'W' AND tg.opp_score > 0", "score", "ASC",
                                   "season DESC, week DESC, owner"), LIST_SIZE),
        lambda db: db.fetch(ranked(MARGINS.format(extra="AND m.home_score > 0 AND m.away_score > 0"),
                                   "margin", "DESC", "season, week"), LIST_SIZE),
        lambda db: db.fetch(ranked(CLOSEST_GAMES, "margin", "ASC", "season, week"), LIST_SIZE),
        draft_tendencies,
    )
    streak_rows = without(streaks, "won", "place")
    return {
        # all-time standings: owners with at least one finished season
        "standings":        [c for c in careers if c["seasons_played"]],
        "highest_season":   season_points(without(high_season)),
        "lowest_season":    season_points(without(low_season)),
        "best_week":        scores(without(best_week)),
        "worst_week":       scores(without(worst_week)),
        "win_streaks":      [r for r, s in zip(streak_rows, streaks) if s["won"] == 1],
        "loss_streaks":     [r for r, s in zip(streak_rows, streaks) if s["won"] == 0],
        "highest_losing":   scores(without(losing)),
        "lowest_winning":   scores(without(winning)),
        "blowouts":         margins(without(blowouts)),
        "closest":          margins(without(closest)),
        "draft_tendencies": tendencies,
    }


@router.get("/luck")
async def get_career_luck():
    """Career luck per owner over every season, the in-progress one included (definition: LUCK_WEEKS
    in seasons.py). Summed from the weeks, so it isn't the sum of the rounded season values."""
    (rows,) = await concurrently(career_luck)
    return rows
