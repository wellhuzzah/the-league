from fastapi import APIRouter, HTTPException
from app.database import get_pool

router = APIRouter(
    prefix="/boxscores",
    tags=["boxscores"]
)


@router.get("/top-performances")
async def get_top_performances(limit: int = 25, position: str = None):
    """Highest single-game scores by any player across all seasons."""
    async with (await get_pool()).acquire() as db:
        if position:
            rows = await db.fetch("""
                SELECT
                    bs.player_name,
                    bs.position,
                    bs.points_scored,
                    bs.season,
                    bs.week,
                    bs.is_starter,
                    t.owner,
                    t.team_id,
                    m.is_playoffs,
                    opp.owner AS opponent
                FROM box_scores bs
                JOIN teams t ON bs.team_id = t.team_id
                JOIN matchups m ON bs.matchup_id = m.id
                JOIN teams opp ON opp.team_id = CASE
                    WHEN m.home_team_id = bs.team_id THEN m.away_team_id
                    ELSE m.home_team_id
                END
                WHERE bs.position = $2
                ORDER BY bs.points_scored DESC
                LIMIT $1
            """, limit, position)
        else:
            rows = await db.fetch("""
                SELECT
                    bs.player_name,
                    bs.position,
                    bs.points_scored,
                    bs.season,
                    bs.week,
                    bs.is_starter,
                    t.owner,
                    t.team_id,
                    m.is_playoffs,
                    opp.owner AS opponent
                FROM box_scores bs
                JOIN teams t ON bs.team_id = t.team_id
                JOIN matchups m ON bs.matchup_id = m.id
                JOIN teams opp ON opp.team_id = CASE
                    WHEN m.home_team_id = bs.team_id THEN m.away_team_id
                    ELSE m.home_team_id
                END
                ORDER BY bs.points_scored DESC
                LIMIT $1
            """, limit)
        return [
            {**dict(row), "points_scored": float(row["points_scored"])}
            for row in rows
        ]


@router.get("/position-breakdown")
async def get_position_breakdown():
    """Average points scored by position per season across all teams."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                season,
                position,
                ROUND(AVG(points_scored)::numeric, 2)   AS avg_points,
                ROUND(MAX(points_scored)::numeric, 2)   AS max_points,
                COUNT(*)                                AS total_games
            FROM box_scores
            WHERE position != 'UNK'
            GROUP BY season, position
            ORDER BY season, position
        """)
        seasons = {}
        for row in rows:
            s = row["season"]
            if s not in seasons:
                seasons[s] = {"season": s, "positions": []}
            seasons[s]["positions"].append({
                "position":    row["position"],
                "avg_points":  float(row["avg_points"]),
                "max_points":  float(row["max_points"]),
                "total_games": row["total_games"],
            })
        return list(seasons.values())


@router.get("/team/{team_id}")
async def get_team_boxscores(team_id: int, season: int = None):
    """All box score entries for a team, optionally filtered by season."""
    async with (await get_pool()).acquire() as db:
        team = await db.fetchrow("SELECT owner FROM teams WHERE team_id = $1", team_id)
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        if season:
            rows = await db.fetch("""
                SELECT
                    bs.season,
                    bs.week,
                    bs.player_name,
                    bs.position,
                    bs.points_scored,
                    bs.is_starter,
                    m.is_playoffs
                FROM box_scores bs
                JOIN matchups m ON bs.matchup_id = m.id
                WHERE bs.team_id = $1 AND bs.season = $2
                ORDER BY bs.week, bs.points_scored DESC
            """, team_id, season)
        else:
            rows = await db.fetch("""
                SELECT
                    bs.season,
                    bs.week,
                    bs.player_name,
                    bs.position,
                    bs.points_scored,
                    bs.is_starter,
                    m.is_playoffs
                FROM box_scores bs
                JOIN matchups m ON bs.matchup_id = m.id
                WHERE bs.team_id = $1
                ORDER BY bs.season, bs.week, bs.points_scored DESC
            """, team_id)

        return {
            "team_id": team_id,
            "owner":   team["owner"],
            "entries": [
                {**dict(row), "points_scored": float(row["points_scored"])}
                for row in rows
            ]
        }


@router.get("/team/{team_id}/best-weeks")
async def get_team_best_weeks(team_id: int, limit: int = 10):
    """A team's best individual player performances ever."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                bs.season,
                bs.week,
                bs.player_name,
                bs.position,
                bs.points_scored,
                bs.is_starter,
                m.is_playoffs,
                opp.owner AS opponent
            FROM box_scores bs
            JOIN matchups m ON bs.matchup_id = m.id
            JOIN teams opp ON opp.team_id = CASE
                WHEN m.home_team_id = $1 THEN m.away_team_id
                ELSE m.home_team_id
            END
            WHERE bs.team_id = $1
            ORDER BY bs.points_scored DESC
            LIMIT $2
        """, team_id, limit)
        return [
            {**dict(row), "points_scored": float(row["points_scored"])}
            for row in rows
        ]


@router.get("/team/{team_id}/position-totals")
async def get_team_position_totals(team_id: int):
    """Points scored by position per season for a team."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                bs.season,
                bs.position,
                ROUND(SUM(bs.points_scored)::numeric, 1)  AS total_points,
                ROUND(AVG(bs.points_scored)::numeric, 2)  AS avg_points,
                COUNT(*)                                   AS appearances
            FROM box_scores bs
            WHERE bs.team_id = $1 AND bs.position != 'UNK'
            GROUP BY bs.season, bs.position
            ORDER BY bs.season, total_points DESC
        """, team_id)

        seasons = {}
        for row in rows:
            s = row["season"]
            if s not in seasons:
                seasons[s] = {"season": s, "positions": []}
            seasons[s]["positions"].append({
                "position":    row["position"],
                "total_points": float(row["total_points"]),
                "avg_points":   float(row["avg_points"]),
                "appearances":  row["appearances"],
            })
        return list(seasons.values())


@router.get("/player/{player_name}/history")
async def get_player_history(player_name: str):
    """All appearances of a player across all seasons and teams. Case-insensitive partial match."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                bs.season,
                bs.week,
                bs.player_name,
                bs.position,
                bs.points_scored,
                bs.is_starter,
                t.owner,
                t.team_id,
                m.is_playoffs
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.player_name ILIKE $1
            ORDER BY bs.points_scored DESC
        """, f"%{player_name}%")

        if not rows:
            return {"query": player_name, "results": [], "total_appearances": 0}

        owner_totals = {}
        for row in rows:
            o = row["owner"]
            if o not in owner_totals:
                owner_totals[o] = {"owner": o, "team_id": row["team_id"], "appearances": 0, "total_points": 0.0}
            owner_totals[o]["appearances"] += 1
            owner_totals[o]["total_points"] = round(owner_totals[o]["total_points"] + float(row["points_scored"]), 1)

        return {
            "query":             player_name,
            "total_appearances": len(rows),
            "by_owner":          sorted(owner_totals.values(), key=lambda x: x["total_points"], reverse=True),
            "results": [
                {**dict(row), "points_scored": float(row["points_scored"])}
                for row in rows
            ]
        }


@router.get("/week/{season}/{week}")
async def get_week_boxscores(season: int, week: int):
    """All player scores for every team in a given week."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                bs.player_name,
                bs.position,
                bs.points_scored,
                bs.is_starter,
                t.owner,
                t.team_id
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            WHERE bs.season = $1 AND bs.week = $2
            ORDER BY t.owner, bs.points_scored DESC
        """, season, week)

        if not rows:
            raise HTTPException(status_code=404, detail="No box score data for this week")

        teams = {}
        for row in rows:
            o = row["owner"]
            if o not in teams:
                teams[o] = {"owner": o, "team_id": row["team_id"], "players": []}
            teams[o]["players"].append({
                "player_name":  row["player_name"],
                "position":     row["position"],
                "points_scored": float(row["points_scored"]),
                "is_starter":   row["is_starter"],
            })

        return {
            "season": season,
            "week":   week,
            "teams":  list(teams.values())
        }
