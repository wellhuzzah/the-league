from fastapi import APIRouter, HTTPException
from app.database import concurrently, get_pool
from app.nfl import season_teams
from app.routers.teams import win_pct

router = APIRouter(
    prefix="/seasons",
    tags=["seasons"]
)


# Seasons with matchups but no records rows are in progress: espn_import.py never writes
# records, so they only exist for finished seasons. Their standings are computed from
# regular-season matchups and nothing is written back.
IN_PROGRESS_STANDINGS = """
    WITH games AS (
        SELECT tg.team_id, tg.score AS pf, tg.opp_score AS pa, tg.result, tg.week
        FROM team_games tg
        WHERE tg.season = $1 AND NOT tg.is_playoffs
          AND tg.score IS NOT NULL AND tg.opp_score IS NOT NULL
    )
    SELECT
        t.team_id,
        t.owner,
        COUNT(*) FILTER (WHERE g.result = 'W')                                  AS wins,
        COUNT(*) FILTER (WHERE g.result = 'L')                                  AS losses,
        COUNT(*) FILTER (WHERE g.result = 'T')                                  AS draws,
        COALESCE(SUM(g.pf), 0)                                                  AS points_for,
        COALESCE(SUM(g.pa), 0)                                                  AS points_against,
        COUNT(g.team_id)                                                        AS games_played,
        MAX(g.week)                                                             AS through_week
    FROM espn_team_map etm
    JOIN teams t ON t.team_id = etm.team_id
    LEFT JOIN games g ON g.team_id = t.team_id
    WHERE etm.season = $1
    GROUP BY t.team_id, t.owner
"""

# Luck (the Bullshit Index), per team-week: the actual result (W 1, T 0.5, L 0, from the stored
# matchup result, so it follows any later decision on a recorded winner) minus the expected result,
# the share of that week's pool the team outscored, an equal score counting half. The pool is every
# regular-season game and, in the playoffs, the playoff games (matchups.playoff_tier: the winners
# bracket and its 3rd/5th/7th-place ladder): toilet bowl games and byes count for nothing, and a game where either side scored 0 drops both
# teams. So every week sums to 0 across the league. Season and career luck are sums over weeks.
LUCK_WEEKS = """
    WITH games AS (
        SELECT tg.season, tg.week, tg.team_id, tg.score,
               CASE tg.result WHEN 'W' THEN 1 WHEN 'T' THEN 0.5 ELSE 0 END AS actual
        FROM team_games tg
        WHERE (NOT tg.is_playoffs OR tg.playoff_tier IN ('WINNERS_BRACKET', 'WINNERS_CONSOLATION_LADDER'))
          AND tg.score > 0 AND tg.opp_score > 0 {where}
    ),
    weekly AS (
        SELECT season, week, team_id, actual,
               (rank() OVER (PARTITION BY season, week ORDER BY score) - 1                -- outscored
                + (count(*) OVER (PARTITION BY season, week, score) - 1) / 2.0)          -- equal: half
               / NULLIF(count(*) OVER (PARTITION BY season, week) - 1, 0) AS expected
        FROM games
    )
"""

# Ties in luck: more actual wins first, then owner.
LUCK_SQL = LUCK_WEEKS + """
    SELECT w.season, w.team_id, t.owner,
           SUM(w.actual)                      AS actual_wins,
           ROUND(SUM(w.expected), 2)          AS expected_wins,
           ROUND(SUM(w.actual - w.expected), 2) AS luck
    FROM weekly w
    JOIN teams t ON t.team_id = w.team_id
    GROUP BY w.season, w.team_id, t.owner
    ORDER BY w.season DESC, luck DESC, actual_wins DESC, t.owner
"""

# Career luck per owner over every season, the in-progress one included (as the all-time records
# are); bps is luck per season.
CAREER_LUCK_SQL = LUCK_WEEKS + """
    SELECT t.owner,
           count(DISTINCT w.season)           AS seasons,
           SUM(w.actual)                      AS actual_wins,
           ROUND(SUM(w.expected), 2)          AS expected_wins,
           ROUND(SUM(w.actual - w.expected), 2) AS luck,
           ROUND(SUM(w.actual - w.expected) / count(DISTINCT w.season), 2) AS bps
    FROM weekly w
    JOIN teams t ON t.team_id = w.team_id
    GROUP BY t.owner
    ORDER BY luck DESC, t.owner
"""


def luck_entry(row):
    return {"team_id": row["team_id"], "owner": row["owner"], "actual_wins": row["actual_wins"],
            "expected_wins": row["expected_wins"], "luck": row["luck"]}


async def season_luck(db, year):
    return [luck_entry(r) for r in await db.fetch(LUCK_SQL.format(where="AND tg.season = $1"), year)]


async def career_luck(db):
    return [dict(r) for r in await db.fetch(CAREER_LUCK_SQL.format(where=""))]


def rank_in_progress(rows):
    """Current place: wins with a draw counting half (the NFL win% order, as every team has played the
    same number of games), then points for, then owner name."""
    ranked = sorted(rows, key=lambda r: (-(r["wins"] + 0.5 * r["draws"]), -float(r["points_for"]), r["owner"]))
    through = max((r["through_week"] or 0 for r in rows), default=0)
    return [
        {
            "team_id":        r["team_id"],
            "owner":          r["owner"],
            "wins":           r["wins"],
            "losses":         r["losses"],
            "draws":          r["draws"],
            "win_pct":        win_pct(r["wins"], r["losses"], r["draws"]),
            "points_for":     float(r["points_for"]),
            "points_against": float(r["points_against"]),
            "final_standing": place,
            "championship":   False,
            "sacko":          False,
            "most_points":    False,
            "in_progress":    True,
            "games_played":   r["games_played"],
            "through_week":   through,
        }
        for place, r in enumerate(ranked, 1)
    ]


@router.get("/")
async def get_seasons():
    """List of all seasons with champion and basic info. A season with matchups but no records
    rows is listed as in_progress, with no champion or sacko yet."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                r.season,
                COUNT(*)                                            AS teams,
                t_champ.owner                                       AS champion,
                t_sacko.owner                                       AS sacko,
                MAX(r.points_for)                                   AS highest_pf
            FROM records r
            LEFT JOIN records r_champ ON r_champ.season = r.season
                AND r_champ.championship = TRUE
            LEFT JOIN teams t_champ ON t_champ.team_id = r_champ.team_id
            LEFT JOIN records r_sacko ON r_sacko.season = r.season
                AND r_sacko.sacko = TRUE
            LEFT JOIN teams t_sacko ON t_sacko.team_id = r_sacko.team_id
            GROUP BY r.season, t_champ.owner, t_sacko.owner
            ORDER BY r.season DESC
        """)
        seasons = [
            {**dict(row), "highest_pf": float(row["highest_pf"] or 0), "in_progress": False}
            for row in rows
        ]
        finished = {int(s["season"]) for s in seasons}
        for (year,) in await db.fetch("SELECT DISTINCT season::int FROM matchups ORDER BY 1 DESC"):
            if year in finished:
                continue
            standings = await db.fetch(IN_PROGRESS_STANDINGS, year)
            if not standings:
                continue
            seasons.append({
                "season":      year,
                "teams":       len(standings),
                "champion":    None,
                "sacko":       None,
                "highest_pf":  max(float(r["points_for"]) for r in standings),
                "in_progress": True,
            })
        return sorted(seasons, key=lambda s: s["season"], reverse=True)


async def season_standings(db, year):
    """A finished season reads records; an in-progress one (no records rows) is computed from its
    regular-season matchups and flagged in_progress, with final_standing as the current place and
    championship/sacko/most_points left false. [] when the season has neither."""
    rows = await db.fetch("""
        SELECT
            t.team_id,
            t.owner,
            r.wins,
            r.losses,
            r.draws,
            r.points_for,
            r.points_against,
            r.final_standing,
            r.championship,
            r.sacko,
            r.most_points
        FROM records r
        JOIN teams t ON r.team_id = t.team_id
        WHERE r.season = $1
        ORDER BY r.final_standing
    """, year)
    if not rows:
        live = await db.fetch(IN_PROGRESS_STANDINGS, year)
        if not live or not any(r["games_played"] for r in live):
            return []
        return rank_in_progress(live)
    return [
        {
            **dict(row),
            "win_pct":        win_pct(row["wins"], row["losses"], row["draws"]),
            "points_for":     float(row["points_for"]),
            "points_against": float(row["points_against"]),
            "in_progress":    False,
        }
        for row in rows
    ]


async def weekly_scores(db, year):
    """Every team's score for every week in a season; adjustment is a league score adjustment
    included in it (0 almost always)."""
    rows = await db.fetch("""
        SELECT
            tg.week,
            tg.is_playoffs,
            t.owner,
            t.team_id,
            tg.score,
            tg.adjustment,
            CASE tg.result WHEN 'W' THEN true WHEN 'L' THEN false END       AS won
        FROM team_games tg
        JOIN teams t ON t.team_id = tg.team_id
        WHERE tg.season = $1
        ORDER BY tg.week, t.owner
    """, year)
    return [{**dict(row), "score": float(row["score"]), "adjustment": float(row["adjustment"])} for row in rows]


async def top_scorer(db, year):
    """Highest-scoring individual player for one owner (starters only, regular season), or None
    before box scores (2018). By ESPN player id, under the player's latest name that season."""
    row = await db.fetchrow("""
        SELECT
            bs.espn_player_id,
            (ARRAY_AGG(bs.player_name ORDER BY bs.week DESC))[1] AS player_name,
            MODE() WITHIN GROUP (ORDER BY bs.position)          AS position,
            t.owner,
            SUM(bs.points_scored) AS total_points
        FROM box_scores bs
        JOIN teams t ON bs.team_id = t.team_id
        JOIN matchups m ON bs.matchup_id = m.id
        WHERE bs.season = $1
          AND bs.is_starter = TRUE
          AND NOT m.is_playoffs
        GROUP BY bs.espn_player_id, t.owner
        ORDER BY total_points DESC, player_name, position, t.owner
        LIMIT 1
    """, year)
    if not row:
        return None
    scorer = {
        "espn_player_id": row["espn_player_id"],
        "player_name":  row["player_name"],
        "position":     row["position"],
        "owner":        row["owner"],
        "total_points": float(row["total_points"]),
    }
    # every NFL team he had that season, "OAK→DAL" (app/nfl.py)
    return (await season_teams([scorer], db, season=year))[0]


@router.get("/{year}")
async def get_season(year: int):
    """Everything the Season page shows, in one call: standings, every team's weekly scores, the
    luck table and the top scorer. A part with no data is [] (or null for top_scorer); 404 only
    when the season has nothing at all."""
    standings, weekly, luck, scorer = await concurrently(
        lambda db: season_standings(db, year),
        lambda db: weekly_scores(db, year),
        lambda db: season_luck(db, year),
        lambda db: top_scorer(db, year),
    )
    if not (standings or weekly or luck or scorer):
        raise HTTPException(status_code=404, detail="Season not found")
    return {"season": year, "standings": standings, "weekly_scores": weekly, "luck": luck,
            "top_scorer": scorer}
