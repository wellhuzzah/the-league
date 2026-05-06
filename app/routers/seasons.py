from fastapi import APIRouter
from app.database import get_pool

router = APIRouter(
    prefix="/seasons",
    tags=["seasons"]
)

@router.get("/{year}/standings")
async def get_standings(year: int):
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT t.owner, r.wins, r.losses, r.draws,
                r.points_for, r.points_against,
                r.final_standing, r.championship,
                r.sacko, r.most_points
            FROM records r
            JOIN teams t ON r.team_id = t.team_id
            WHERE r.season = $1
            ORDER BY r.final_standing
        """, year)
        return [dict(row) for row in rows]

@router.get("/")
async def get_seasons():
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT season, COUNT(*) as teams
            FROM records
            GROUP BY season
            ORDER BY season DESC
        """)
        return [dict(row) for row in rows]
