from fastapi import APIRouter
from app.database import get_pool

router = APIRouter(
    prefix="/records",
    tags=["records"]
)

@router.get("/alltime")
async def get_alltime_standings():
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                t.owner,
                COUNT(r.id) as seasons_played,
                SUM(r.wins) as total_wins,
                SUM(r.losses) as total_losses,
                SUM(r.draws) as total_draws,
                SUM(r.points_for) as total_points_for,
                SUM(r.points_against) as total_points_against,
                SUM(CASE WHEN r.championship THEN 1 ELSE 0 END) as championships,
                SUM(CASE WHEN r.sacko THEN 1 ELSE 0 END) as sackos,
                SUM(CASE WHEN r.most_points THEN 1 ELSE 0 END) as most_points_seasons
            FROM records r
            JOIN teams t ON r.team_id = t.team_id
            GROUP BY t.owner
            ORDER BY total_wins DESC
        """)
        return [
            {
                **dict(row),
                "total_points_for": float(row["total_points_for"]),
                "total_points_against": float(row["total_points_against"]),
                "win_pct": round(row["total_wins"] / (row["total_wins"] + row["total_losses"]), 3)
            }
            for row in rows
        ]
