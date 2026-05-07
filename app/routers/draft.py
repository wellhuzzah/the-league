from fastapi import APIRouter, HTTPException
from app.database import get_pool

router = APIRouter(
    prefix="/draft",
    tags=["draft"]
)


@router.get("/")
async def get_draft_seasons():
    """List of all seasons with draft data available."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                season,
                COUNT(*)                        AS total_picks,
                COUNT(CASE WHEN is_keeper THEN 1 END) AS keeper_picks,
                MAX(round_num)                  AS rounds
            FROM draft_picks
            GROUP BY season
            ORDER BY season DESC
        """)
        return [dict(row) for row in rows]


@router.get("/player/{player_name}")
async def search_player_draft_history(player_name: str):
    """
    Search how many times and by whom a player has been drafted.
    Case-insensitive partial match.
    """
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                dp.season,
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.player_name,
                dp.position,
                dp.is_keeper,
                t.owner,
                t.team_id
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            WHERE dp.player_name ILIKE $1
            ORDER BY dp.season, dp.overall_pick
        """, f"%{player_name}%")

        if not rows:
            return {"query": player_name, "results": [], "times_drafted": 0}

        return {
            "query":         player_name,
            "times_drafted": len(rows),
            "results": [
                {
                    "season":        row["season"],
                    "overall_pick":  row["overall_pick"],
                    "round_num":     row["round_num"],
                    "pick_in_round": row["pick_in_round"],
                    "player_name":   row["player_name"],
                    "position":      row["position"],
                    "is_keeper":     row["is_keeper"],
                    "owner":         row["owner"],
                    "team_id":       row["team_id"],
                }
                for row in rows
            ]
        }


@router.get("/{year}")
async def get_draft(year: int):
    """Full draft board for a season."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.player_name,
                dp.position,
                dp.is_keeper,
                dp.espn_player_id,
                t.owner,
                t.team_id
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            WHERE dp.season = $1
            ORDER BY dp.overall_pick
        """, year)
        if not rows:
            raise HTTPException(status_code=404, detail="Draft not found")

        rounds = {}
        for row in rows:
            r = row["round_num"]
            if r not in rounds:
                rounds[r] = []
            rounds[r].append({
                "overall_pick":  row["overall_pick"],
                "pick_in_round": row["pick_in_round"],
                "player_name":   row["player_name"],
                "position":      row["position"],
                "is_keeper":     row["is_keeper"],
                "owner":         row["owner"],
                "team_id":       row["team_id"],
            })

        return {
            "season": year,
            "rounds": [
                {"round": r, "picks": picks}
                for r, picks in sorted(rounds.items())
            ]
        }


@router.get("/{year}/by-team")
async def get_draft_by_team(year: int):
    """Draft picks for a season organized by team."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.player_name,
                dp.position,
                dp.is_keeper,
                t.owner,
                t.team_id
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            WHERE dp.season = $1
            ORDER BY t.owner, dp.overall_pick
        """, year)
        if not rows:
            raise HTTPException(status_code=404, detail="Draft not found")

        teams = {}
        for row in rows:
            owner = row["owner"]
            if owner not in teams:
                teams[owner] = {
                    "owner":   owner,
                    "team_id": row["team_id"],
                    "picks":   []
                }
            teams[owner]["picks"].append({
                "overall_pick":  row["overall_pick"],
                "round_num":     row["round_num"],
                "pick_in_round": row["pick_in_round"],
                "player_name":   row["player_name"],
                "position":      row["position"],
                "is_keeper":     row["is_keeper"],
            })

        return {"season": year, "teams": list(teams.values())}


@router.get("/{year}/keepers")
async def get_keepers(year: int):
    """All keeper picks for a season."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.player_name,
                dp.position,
                t.owner,
                t.team_id
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            WHERE dp.season = $1 AND dp.is_keeper = TRUE
            ORDER BY dp.overall_pick
        """, year)

        return {
            "season":  year,
            "keepers": [dict(row) for row in rows]
        }
