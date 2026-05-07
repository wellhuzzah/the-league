from fastapi import APIRouter
from app.database import get_pool

router = APIRouter(
    prefix="/records",
    tags=["records"]
)


@router.get("/alltime")
async def get_alltime_standings():
    """Career standings for all owners."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                t.owner,
                t.team_id,
                COUNT(r.id)                                         AS seasons_played,
                SUM(r.wins)                                         AS total_wins,
                SUM(r.losses)                                       AS total_losses,
                SUM(r.draws)                                        AS total_draws,
                SUM(r.points_for)                                   AS total_points_for,
                SUM(r.points_against)                               AS total_points_against,
                SUM(CASE WHEN r.championship THEN 1 ELSE 0 END)    AS championships,
                SUM(CASE WHEN r.sacko        THEN 1 ELSE 0 END)    AS sackos,
                SUM(CASE WHEN r.most_points  THEN 1 ELSE 0 END)    AS most_points_seasons
            FROM records r
            JOIN teams t ON r.team_id = t.team_id
            GROUP BY t.owner, t.team_id
            ORDER BY total_wins DESC
        """)
        return [
            {
                **dict(row),
                "total_points_for":     float(row["total_points_for"]),
                "total_points_against": float(row["total_points_against"]),
                "win_pct": round(
                    row["total_wins"] /
                    max(row["total_wins"] + row["total_losses"], 1),
                    3
                ),
            }
            for row in rows
        ]


@router.get("/scoring/highest-season")
async def get_highest_season_scores(limit: int = 10):
    """Highest points_for totals for a single season."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
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
            ORDER BY r.points_for DESC
            LIMIT $1
        """, limit)
        return [
            {**dict(row), "points_for": float(row["points_for"]),
             "points_against": float(row["points_against"])}
            for row in rows
        ]


@router.get("/scoring/lowest-season")
async def get_lowest_season_scores(limit: int = 10):
    """Lowest points_for totals for a single season."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
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
            ORDER BY r.points_for ASC
            LIMIT $1
        """, limit)
        return [
            {**dict(row), "points_for": float(row["points_for"]),
             "points_against": float(row["points_against"])}
            for row in rows
        ]


@router.get("/scoring/best-single-week")
async def get_best_single_week(limit: int = 10):
    """Highest individual week scores all time."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                t.owner,
                t.team_id,
                CASE WHEN m.home_team_id = t.team_id
                     THEN m.home_score ELSE m.away_score END        AS score,
                CASE WHEN m.home_team_id = t.team_id
                     THEN m.away_score ELSE m.home_score END        AS opp_score,
                t_opp.owner                                         AS opponent
            FROM matchups m
            JOIN teams t ON t.team_id IN (m.home_team_id, m.away_team_id)
            JOIN teams t_opp ON t_opp.team_id = CASE
                WHEN m.home_team_id = t.team_id THEN m.away_team_id
                ELSE m.home_team_id
            END
            ORDER BY score DESC
            LIMIT $1
        """, limit)
        return [
            {**dict(row), "score": float(row["score"]),
             "opp_score": float(row["opp_score"])}
            for row in rows
        ]


@router.get("/scoring/worst-single-week")
async def get_worst_single_week(limit: int = 10):
    """Lowest individual week scores all time."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                t.owner,
                t.team_id,
                CASE WHEN m.home_team_id = t.team_id
                     THEN m.home_score ELSE m.away_score END        AS score,
                CASE WHEN m.home_team_id = t.team_id
                     THEN m.away_score ELSE m.home_score END        AS opp_score,
                t_opp.owner                                         AS opponent
            FROM matchups m
            JOIN teams t ON t.team_id IN (m.home_team_id, m.away_team_id)
            JOIN teams t_opp ON t_opp.team_id = CASE
                WHEN m.home_team_id = t.team_id THEN m.away_team_id
                ELSE m.home_team_id
            END
            ORDER BY score ASC
            LIMIT $1
        """, limit)
        return [
            {**dict(row), "score": float(row["score"]),
             "opp_score": float(row["opp_score"])}
            for row in rows
        ]


@router.get("/streaks/wins")
async def get_win_streaks(limit: int = 10):
    """Longest winning streaks all time across all seasons."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            WITH ordered_games AS (
                SELECT
                    CASE WHEN home_team_id = t.team_id THEN home_team_id
                         ELSE away_team_id END                      AS team_id,
                    t.owner,
                    season,
                    week,
                    CASE WHEN winner_team_id = t.team_id THEN 1
                         WHEN winner_team_id IS NULL THEN NULL
                         ELSE 0 END                                 AS won,
                    ROW_NUMBER() OVER (
                        PARTITION BY t.team_id
                        ORDER BY season, week
                    )                                               AS rn
                FROM matchups m
                CROSS JOIN teams t
                WHERE m.home_team_id = t.team_id OR m.away_team_id = t.team_id
            ),
            grouped AS (
                SELECT
                    team_id,
                    owner,
                    season,
                    week,
                    won,
                    rn,
                    rn - ROW_NUMBER() OVER (
                        PARTITION BY team_id, won
                        ORDER BY rn
                    )                                               AS grp
                FROM ordered_games
                WHERE won IS NOT NULL
            ),
            streaks AS (
                SELECT
                    team_id,
                    owner,
                    won,
                    COUNT(*)                                        AS streak_length,
                    MIN(season)                                     AS start_season,
                    MIN(week)                                       AS start_week,
                    MAX(season)                                     AS end_season,
                    MAX(week)                                       AS end_week
                FROM grouped
                GROUP BY team_id, owner, won, grp
            )
            SELECT
                owner,
                team_id,
                streak_length,
                start_season,
                start_week,
                end_season,
                end_week
            FROM streaks
            WHERE won = 1
            ORDER BY streak_length DESC
            LIMIT $1
        """, limit)
        return [dict(row) for row in rows]


@router.get("/streaks/losses")
async def get_loss_streaks(limit: int = 10):
    """Longest losing streaks all time across all seasons."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            WITH ordered_games AS (
                SELECT
                    CASE WHEN home_team_id = t.team_id THEN home_team_id
                         ELSE away_team_id END                      AS team_id,
                    t.owner,
                    season,
                    week,
                    CASE WHEN winner_team_id = t.team_id THEN 1
                         WHEN winner_team_id IS NULL THEN NULL
                         ELSE 0 END                                 AS won,
                    ROW_NUMBER() OVER (
                        PARTITION BY t.team_id
                        ORDER BY season, week
                    )                                               AS rn
                FROM matchups m
                CROSS JOIN teams t
                WHERE m.home_team_id = t.team_id OR m.away_team_id = t.team_id
            ),
            grouped AS (
                SELECT
                    team_id,
                    owner,
                    season,
                    week,
                    won,
                    rn,
                    rn - ROW_NUMBER() OVER (
                        PARTITION BY team_id, won
                        ORDER BY rn
                    )                                               AS grp
                FROM ordered_games
                WHERE won IS NOT NULL
            ),
            streaks AS (
                SELECT
                    team_id,
                    owner,
                    won,
                    COUNT(*)                                        AS streak_length,
                    MIN(season)                                     AS start_season,
                    MIN(week)                                       AS start_week,
                    MAX(season)                                     AS end_season,
                    MAX(week)                                       AS end_week
                FROM grouped
                GROUP BY team_id, owner, won, grp
            )
            SELECT
                owner,
                team_id,
                streak_length,
                start_season,
                start_week,
                end_season,
                end_week
            FROM streaks
            WHERE won = 0
            ORDER BY streak_length DESC
            LIMIT $1
        """, limit)
        return [dict(row) for row in rows]


@router.get("/draft/most-drafted-position")
async def get_most_drafted_by_position():
    """Position breakdown by round across all seasons."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                round_num,
                position,
                COUNT(*)        AS times_drafted
            FROM draft_picks
            WHERE position IS NOT NULL
            GROUP BY round_num, position
            ORDER BY round_num, times_drafted DESC
        """)
        rounds = {}
        for row in rows:
            r = row["round_num"]
            if r not in rounds:
                rounds[r] = []
            rounds[r].append({
                "position":      row["position"],
                "times_drafted": row["times_drafted"]
            })
        return [{"round": r, "positions": positions}
                for r, positions in sorted(rounds.items())]


@router.get("/draft/by-owner")
async def get_draft_by_owner():
    """Total picks by position per owner across all seasons."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                t.owner,
                t.team_id,
                dp.position,
                COUNT(*)        AS times_drafted
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            WHERE dp.position IS NOT NULL
            GROUP BY t.owner, t.team_id, dp.position
            ORDER BY t.owner, times_drafted DESC
        """)
        owners = {}
        for row in rows:
            o = row["owner"]
            if o not in owners:
                owners[o] = {"owner": o, "team_id": row["team_id"], "positions": {}}
            owners[o]["positions"][row["position"]] = row["times_drafted"]
        return list(owners.values())
