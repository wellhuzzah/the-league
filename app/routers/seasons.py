from fastapi import APIRouter, HTTPException
from app.database import get_pool

router = APIRouter(
    prefix="/seasons",
    tags=["seasons"]
)


# Seasons with matchups but no records rows are in progress: espn_import.py never writes
# records, so they only exist for finished seasons. Their standings are computed from
# regular-season matchups and nothing is written back.
IN_PROGRESS_STANDINGS = """
    WITH games AS (
        SELECT m.home_team_id AS team_id, m.home_score AS pf, m.away_score AS pa,
               m.winner_team_id AS winner, m.week
        FROM matchups m WHERE m.season = $1 AND NOT m.is_playoffs
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        UNION ALL
        SELECT m.away_team_id, m.away_score, m.home_score, m.winner_team_id, m.week
        FROM matchups m WHERE m.season = $1 AND NOT m.is_playoffs
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
    )
    SELECT
        t.team_id,
        t.owner,
        COUNT(*) FILTER (WHERE g.winner = g.team_id)                            AS wins,
        COUNT(*) FILTER (WHERE g.winner IS NOT NULL AND g.winner <> g.team_id)  AS losses,
        COUNT(*) FILTER (WHERE g.team_id IS NOT NULL AND g.winner IS NULL)      AS draws,
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


def rank_in_progress(rows):
    """Current place: win% (a draw counts half), then points for, then owner name."""
    ranked = sorted(rows, key=lambda r: (-(r["wins"] + 0.5 * r["draws"]), -float(r["points_for"]), r["owner"]))
    through = max((r["through_week"] or 0 for r in rows), default=0)
    return [
        {
            "team_id":        r["team_id"],
            "owner":          r["owner"],
            "wins":           r["wins"],
            "losses":         r["losses"],
            "draws":          r["draws"],
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


@router.get("/{year}/standings")
async def get_standings(year: int):
    """Full standings for a season. A finished season reads records; an in-progress one (no
    records rows) is computed from its regular-season matchups and flagged in_progress, with
    final_standing as the current place and championship/sacko/most_points left false."""
    async with (await get_pool()).acquire() as db:
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
                raise HTTPException(status_code=404, detail="Season not found")
            return rank_in_progress(live)
        return [
            {
                **dict(row),
                "points_for":     float(row["points_for"]),
                "points_against": float(row["points_against"]),
                "in_progress":    False,
            }
            for row in rows
        ]


@router.get("/{year}/weekly-scores")
async def get_weekly_scores(year: int):
    """Every team's score for every week in a season — useful for charts."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.week,
                m.is_playoffs,
                t.owner,
                t.team_id,
                CASE WHEN m.home_team_id = t.team_id
                     THEN m.home_score ELSE m.away_score END        AS score,
                CASE WHEN m.winner_team_id = t.team_id THEN true
                     WHEN m.winner_team_id IS NULL THEN NULL
                     ELSE false END                                  AS won
            FROM matchups m
            JOIN teams t ON t.team_id IN (m.home_team_id, m.away_team_id)
            WHERE m.season = $1
            ORDER BY m.week, t.owner
        """, year)
        if not rows:
            raise HTTPException(status_code=404, detail="Season not found")
        return [
            {**dict(row), "score": float(row["score"])}
            for row in rows
        ]


@router.get("/{year}/luck")
async def get_season_luck(year: int):
    """
    Luck index for a season — compares actual wins to expected wins
    based on points scored vs rest of league each week.
    A team is 'lucky' if they won games they could have lost against
    the rest of the field.
    """
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            WITH weekly_scores AS (
                SELECT
                    m.week,
                    t.team_id,
                    t.owner,
                    CASE WHEN m.home_team_id = t.team_id
                         THEN m.home_score ELSE m.away_score END    AS score,
                    CASE WHEN m.winner_team_id = t.team_id THEN 1
                         WHEN m.winner_team_id IS NULL THEN 0
                         ELSE 0 END                                  AS actual_win
                FROM matchups m
                JOIN teams t ON t.team_id IN (m.home_team_id, m.away_team_id)
                WHERE m.season = $1 AND m.is_playoffs = FALSE
            ),
            expected AS (
                SELECT
                    ws.team_id,
                    ws.owner,
                    ws.week,
                    ws.score,
                    ws.actual_win,
                    -- Expected wins: fraction of other teams this score would beat
                    (
                        SELECT COUNT(*)::float / NULLIF(COUNT(*) - 1, 0)
                        FROM weekly_scores ws2
                        WHERE ws2.week = ws.week AND ws2.score < ws.score
                    )                                               AS expected_win_frac
                FROM weekly_scores ws
            )
            SELECT
                team_id,
                owner,
                SUM(actual_win)                                     AS actual_wins,
                ROUND(SUM(expected_win_frac)::numeric, 2)           AS expected_wins,
                ROUND((SUM(actual_win) - SUM(expected_win_frac))::numeric, 2) AS luck
            FROM expected
            GROUP BY team_id, owner
            ORDER BY luck DESC
        """, year)
        if not rows:
            raise HTTPException(status_code=404, detail="Season not found")
        return [dict(row) for row in rows]


@router.get("/{year}/top-scorer")
async def get_season_top_scorer(year: int):
    """Highest-scoring individual player (starters only, regular season) for the season."""
    async with (await get_pool()).acquire() as db:
        row = await db.fetchrow("""
            SELECT
                bs.player_name,
                bs.position,
                t.owner,
                SUM(bs.points_scored) AS total_points
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.season = $1
              AND bs.is_starter = TRUE
              AND NOT m.is_playoffs
            GROUP BY bs.player_name, bs.position, t.owner
            ORDER BY total_points DESC, bs.player_name, bs.position, t.owner
            LIMIT 1
        """, year)
        if not row:
            raise HTTPException(status_code=404, detail="No scorer data for this season")
        return {
            "player_name":  row["player_name"],
            "position":     row["position"],
            "owner":        row["owner"],
            "total_points": float(row["total_points"]),
        }
