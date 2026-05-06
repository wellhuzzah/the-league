from fastapi import APIRouter
from app.database import get_pool

router = APIRouter(
    prefix="/draft",
    tags=["draft"]
)

@router.get("/{year}")
async def get_draft_order(year: int):
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT d.pick, t.owner, d.season
            FROM draft_order d
            JOIN teams t ON d.team_id = t.team_id
            WHERE d.season = $1
            ORDER BY d.pick
        """, year)
        return [dict(row) for row in rows]

@router.get("/")
async def get_draft_seasons():
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT DISTINCT season
            FROM draft_order
            ORDER BY season DESC
        """)
        return [dict(row) for row in rows]
