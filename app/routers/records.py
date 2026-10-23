import math

from fastapi import APIRouter
from app.database import concurrently
from app.routers.draft import draft_tendencies
from app.routers.seasons import all_luck
from app.routers.teams import career_totals

router = APIRouter(
    prefix="/records",
    tags=["records"]
)

LIST_SIZE = 10

# One team's side of each game (team_games), with both owners. Ties in score are broken by date
# (direction chosen per list to match the order the old queries happened to return), then owner,
# so the lists no longer change order between requests.
TEAM_WEEKS = """
    SELECT tg.season, tg.week, tg.is_playoffs, t.owner, t.team_id, tg.score, tg.opp_score,
           o.owner AS opponent
    FROM team_games tg
    JOIN teams t ON t.team_id = tg.team_id
    JOIN teams o ON o.team_id = tg.opp_id
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
    ORDER BY r.points_for {direction}
    LIMIT $1
"""

# Winning and losing streaks in one pass. A tie game ends a streak (it takes a row number but
# belongs to neither group). start_week/end_week are MIN/MAX over the streak's weeks, as before,
# so a streak spanning seasons reports the smallest and largest week number, not its first and
# last game's week.
STREAKS = """
    WITH ordered_games AS (
        SELECT tg.team_id, t.owner, tg.season, tg.week,
               CASE tg.result WHEN 'W' THEN 1 WHEN 'L' THEN 0 END              AS won,
               ROW_NUMBER() OVER (PARTITION BY tg.team_id ORDER BY tg.season, tg.week) AS rn
        FROM team_games tg
        JOIN teams t ON t.team_id = tg.team_id
    ),
    grouped AS (
        SELECT team_id, owner, season, week, won,
               rn - ROW_NUMBER() OVER (PARTITION BY team_id, won ORDER BY rn)  AS grp
        FROM ordered_games
        WHERE won IS NOT NULL
    ),
    streaks AS (
        SELECT team_id, owner, won,
               COUNT(*)      AS streak_length,
               MIN(season)   AS start_season,
               MIN(week)     AS start_week,
               MAX(season)   AS end_season,
               MAX(week)     AS end_week
        FROM grouped
        GROUP BY team_id, owner, won, grp
    ),
    ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY won ORDER BY streak_length DESC,
                                     start_season, start_week, owner) AS place
        FROM streaks
    )
    SELECT won, owner, team_id, streak_length, start_season, start_week, end_season, end_week
    FROM ranked
    WHERE place <= $1
    ORDER BY won DESC, place
"""

MARGINS = """
    SELECT
        m.season,
        m.week,
        m.is_playoffs,
        m.home_score,
        m.away_score,
        m.margin,
        t_home.owner AS home_owner,
        t_away.owner AS away_owner,
        t_winner.owner AS winner
    FROM matchups m
    JOIN teams t_home   ON m.home_team_id   = t_home.team_id
    JOIN teams t_away   ON m.away_team_id   = t_away.team_id
    LEFT JOIN teams t_winner ON m.winner_team_id = t_winner.team_id
    WHERE m.winner_team_id IS NOT NULL
      {extra}
    ORDER BY m.margin {direction}
    LIMIT $1
"""


def scores(rows):
    return [{**dict(r), "score": float(r["score"]), "opp_score": float(r["opp_score"])} for r in rows]


def season_points(rows):
    return [{**dict(r), "points_for": float(r["points_for"]), "points_against": float(r["points_against"])}
            for r in rows]


def margins(rows):
    return [{**dict(r), "margin": float(r["margin"]), "home_score": float(r["home_score"]),
             "away_score": float(r["away_score"])} for r in rows]


@router.get("/summary")
async def get_records_summary():
    """Every list on the Records page in one call (it used to make 12)."""
    (careers, high_season, low_season, best_week, worst_week, streaks, losing, winning,
     blowouts, closest, tendencies) = await concurrently(
        career_totals,
        lambda db: db.fetch(SEASON_TOTALS.format(direction="DESC"), LIST_SIZE),
        lambda db: db.fetch(SEASON_TOTALS.format(direction="ASC"), LIST_SIZE),
        lambda db: db.fetch(TEAM_WEEKS + "ORDER BY tg.score DESC, tg.season, tg.week, t.owner LIMIT $1", LIST_SIZE),
        lambda db: db.fetch(TEAM_WEEKS + "ORDER BY tg.score ASC, tg.season, tg.week, t.owner LIMIT $1", LIST_SIZE),
        lambda db: db.fetch(STREAKS, LIST_SIZE),
        # Highest scores that still lost: the unluckiest weeks
        lambda db: db.fetch(TEAM_WEEKS + "WHERE tg.result = 'L' ORDER BY tg.score DESC, tg.season, tg.week, t.owner LIMIT $1", LIST_SIZE),
        # Lowest scores that still won (against a team that scored): the luckiest weeks
        lambda db: db.fetch(TEAM_WEEKS + "WHERE tg.result = 'W' AND tg.opp_score > 0 ORDER BY tg.score ASC, tg.season DESC, tg.week DESC, t.owner LIMIT $1", LIST_SIZE),
        lambda db: db.fetch(MARGINS.format(extra="AND m.home_score > 0 AND m.away_score > 0", direction="DESC"), LIST_SIZE),
        lambda db: db.fetch(MARGINS.format(extra="", direction="ASC"), LIST_SIZE),
        draft_tendencies,
    )
    streak_rows = [dict(r) for r in streaks]
    strip = lambda r: {k: v for k, v in r.items() if k != "won"}
    return {
        # all-time standings: owners with at least one finished season
        "standings":        [c for c in careers if c["seasons_played"]],
        "highest_season":   season_points(high_season),
        "lowest_season":    season_points(low_season),
        "best_week":        scores(best_week),
        "worst_week":       scores(worst_week),
        "win_streaks":      [strip(r) for r in streak_rows if r["won"] == 1],
        "loss_streaks":     [strip(r) for r in streak_rows if r["won"] == 0],
        "highest_losing":   scores(losing),
        "lowest_winning":   scores(winning),
        "blowouts":         margins(blowouts),
        "closest":          margins(closest),
        "draft_tendencies": tendencies,
    }


def js_round_2(x):
    """Math.round(x * 100) / 100 as JavaScript computes it (halves round up), so these numbers are
    exactly what the page used to compute in the browser."""
    v = x * 100
    f = math.floor(v)
    return (f + 1 if v - f >= 0.5 else f) / 100


@router.get("/luck")
async def get_career_luck():
    """Career luck per owner over finished seasons (an in-progress season would shift it weekly).
    Seasons are added newest first, each in luck order, the way the page used to sum them."""
    async def finished_luck(db):
        finished = {r["season"] for r in await db.fetch("SELECT DISTINCT season FROM records")}
        return [r for r in await all_luck(db) if r["season"] in finished]
    (rows,) = await concurrently(finished_luck)
    by_owner = {}
    for r in rows:
        o = by_owner.setdefault(r["owner"], {"owner": r["owner"], "seasons": 0, "actual_wins": 0,
                                             "expected_wins": 0.0, "luck": 0.0})
        o["seasons"] += 1
        o["actual_wins"] += r["actual_wins"]
        o["expected_wins"] += float(r["expected_wins"])
        o["luck"] += float(r["luck"])
    return [
        {**o,
         "expected_wins": js_round_2(o["expected_wins"]),
         "luck":          js_round_2(o["luck"]),
         "bps":           js_round_2(o["luck"] / o["seasons"])}
        for o in by_owner.values()
    ]
