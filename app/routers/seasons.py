from fastapi import APIRouter, HTTPException
from app.database import get_pool

router = APIRouter(
    prefix="/seasons",
    tags=["seasons"]
)


@router.get("/")
async def get_seasons():
    """List of all seasons with champion and basic info."""
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
        return [
            {**dict(row), "highest_pf": float(row["highest_pf"] or 0)}
            for row in rows
        ]


@router.get("/{year}/standings")
async def get_standings(year: int):
    """Full standings for a season."""
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
            raise HTTPException(status_code=404, detail="Season not found")
        return [
            {
                **dict(row),
                "points_for":     float(row["points_for"]),
                "points_against": float(row["points_against"]),
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
    """Highest-scoring individual player (starters only) for the season."""
    async with (await get_pool()).acquire() as db:
        row = await db.fetchrow("""
            SELECT
                bs.player_name,
                bs.position,
                t.owner,
                SUM(bs.points_scored) AS total_points
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            WHERE bs.season = $1
              AND bs.is_starter = TRUE
            GROUP BY bs.player_name, bs.position, t.owner
            ORDER BY total_points DESC
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
