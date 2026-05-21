from collections import defaultdict
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


_POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'DST']


@router.get("/tendencies")
async def get_draft_tendencies():
    """Round-by-round position tendency per owner across all seasons."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            WITH season_counts AS (
                SELECT t.owner, t.team_id, COUNT(DISTINCT dp.season) AS seasons_played
                FROM draft_picks dp
                JOIN teams t ON dp.team_id = t.team_id
                GROUP BY t.owner, t.team_id
            )
            SELECT
                sc.owner,
                sc.team_id,
                sc.seasons_played,
                dp.round_num,
                dp.position,
                COUNT(*) AS pick_count
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            JOIN season_counts sc ON sc.team_id = t.team_id
            WHERE dp.position IS NOT NULL
              AND dp.round_num BETWEEN 1 AND 16
            GROUP BY sc.owner, sc.team_id, sc.seasons_played, dp.round_num, dp.position
            ORDER BY sc.owner, dp.round_num
        """)

        owners: dict = {}
        for row in rows:
            o = row["owner"]
            if o not in owners:
                owners[o] = {
                    "owner":          o,
                    "team_id":        row["team_id"],
                    "seasons_played": row["seasons_played"],
                    "rounds":         {},
                }
            r   = row["round_num"]
            pos = "DST" if row["position"] == "D/ST" else row["position"]
            if r not in owners[o]["rounds"]:
                owners[o]["rounds"][r] = {p: 0 for p in _POSITIONS}
            if pos in owners[o]["rounds"][r]:
                owners[o]["rounds"][r][pos] = row["pick_count"]

        result = []
        for od in owners.values():
            rounds_list = [
                {"round": r, "counts": od["rounds"].get(r, {p: 0 for p in _POSITIONS})}
                for r in range(1, 17)
            ]
            result.append({
                "owner":          od["owner"],
                "team_id":        od["team_id"],
                "seasons_played": od["seasons_played"],
                "rounds":         rounds_list,
            })

        return sorted(result, key=lambda x: x["owner"])


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


@router.get("/value/history")
async def get_value_history(limit: int = 25):
    """All-time best steals and busts across every draft season."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                dp.season,
                dp.player_name,
                dp.position,
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.is_keeper,
                t.owner,
                COALESCE(ROUND(SUM(bs.points_scored)::numeric, 1), 0) AS season_points,
                COUNT(bs.week) AS weeks_active
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            LEFT JOIN box_scores bs
                ON bs.player_name = dp.player_name
               AND bs.season = dp.season
            GROUP BY dp.season, dp.player_name, dp.position, dp.overall_pick,
                     dp.round_num, dp.pick_in_round, dp.is_keeper, t.owner
            ORDER BY dp.season, dp.overall_pick
        """)

        by_season = defaultdict(list)
        for row in rows:
            by_season[row["season"]].append({
                "season":        row["season"],
                "player_name":   row["player_name"],
                "position":      row["position"],
                "overall_pick":  row["overall_pick"],
                "round_num":     row["round_num"],
                "pick_in_round": row["pick_in_round"],
                "is_keeper":     row["is_keeper"],
                "owner":         row["owner"],
                "season_points": float(row["season_points"]),
                "weeks_active":  row["weeks_active"],
            })

        all_picks = []
        for season_picks in by_season.values():
            scored = sorted(
                [p for p in season_picks if p["season_points"] > 0],
                key=lambda p: p["season_points"], reverse=True
            )
            rank_map = {p["player_name"]: i + 1 for i, p in enumerate(scored)}
            for p in season_picks:
                p["scoring_rank"] = rank_map.get(p["player_name"], len(season_picks) + 1)
                p["value_gap"]    = p["overall_pick"] - p["scoring_rank"]
            all_picks.extend(season_picks)

        steals = sorted(
            [p for p in all_picks if p["season_points"] > 0],
            key=lambda p: p["value_gap"], reverse=True
        )[:limit]

        busts = sorted(
            [p for p in all_picks if p["round_num"] <= 2],
            key=lambda p: p["season_points"]
        )[:limit]

        return {"steals": steals, "busts": busts}


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


@router.get("/{year}/value")
async def get_draft_value(year: int):
    """Draft pick value — compares draft position to season scoring for a single year."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                dp.player_name,
                dp.position,
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.is_keeper,
                t.owner,
                COALESCE(ROUND(SUM(bs.points_scored)::numeric, 1), 0) AS season_points,
                COUNT(bs.week) AS weeks_active
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            LEFT JOIN box_scores bs
                ON bs.player_name = dp.player_name
               AND bs.season = dp.season
            WHERE dp.season = $1
            GROUP BY dp.player_name, dp.position, dp.overall_pick, dp.round_num,
                     dp.pick_in_round, dp.is_keeper, t.owner
            ORDER BY dp.overall_pick
        """, year)

        if not rows:
            raise HTTPException(status_code=404, detail="Draft not found")

        picks = [
            {
                "player_name":   row["player_name"],
                "position":      row["position"],
                "overall_pick":  row["overall_pick"],
                "round_num":     row["round_num"],
                "pick_in_round": row["pick_in_round"],
                "is_keeper":     row["is_keeper"],
                "owner":         row["owner"],
                "season_points": float(row["season_points"]),
                "weeks_active":  row["weeks_active"],
            }
            for row in rows
        ]

        scored = sorted(
            [p for p in picks if p["season_points"] > 0],
            key=lambda p: p["season_points"], reverse=True
        )
        rank_map = {p["player_name"]: i + 1 for i, p in enumerate(scored)}

        for p in picks:
            p["scoring_rank"] = rank_map.get(p["player_name"], len(picks) + 1)
            p["value_gap"]    = p["overall_pick"] - p["scoring_rank"]

        steal = max(scored, key=lambda p: p["value_gap"]) if scored else None
        early = [p for p in picks if p["round_num"] <= 2]
        bust  = min(early, key=lambda p: p["season_points"]) if early else None

        return {
            "season": year,
            "picks":  sorted(picks, key=lambda p: p["value_gap"], reverse=True),
            "steal":  steal,
            "bust":   bust,
        }
