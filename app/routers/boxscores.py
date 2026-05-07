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


@router.get("/random-player")
async def get_random_player():
    """
    Returns a random player with highlight stats.
    Pulls from box_scores so we have actual scoring data.
    Minimum 5 appearances to filter out garbage data.
    """
    async with (await get_pool()).acquire() as db:
        # Pick a random player with enough data to be interesting
        player = await db.fetchrow("""
            SELECT
                player_name,
                position,
                COUNT(*)                                        AS appearances,
                ROUND(AVG(points_scored)::numeric, 2)          AS avg_points,
                ROUND(MAX(points_scored)::numeric, 2)          AS best_game,
                ROUND(MIN(points_scored)::numeric, 2)          AS worst_game,
                ROUND(SUM(points_scored)::numeric, 1)          AS total_points
            FROM box_scores
            WHERE position != 'UNK'
            GROUP BY player_name, position
            HAVING COUNT(*) >= 5
            ORDER BY RANDOM()
            LIMIT 1
        """)

        if not player:
            return None

        name = player["player_name"]
        pos  = player["position"]

        # Best single game details
        best_game_row = await db.fetchrow("""
            SELECT
                bs.points_scored,
                bs.season,
                bs.week,
                t.owner,
                m.is_playoffs,
                opp.owner AS opponent
            FROM box_scores bs
            JOIN teams t   ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            JOIN teams opp ON opp.team_id = CASE
                WHEN m.home_team_id = bs.team_id THEN m.away_team_id
                ELSE m.home_team_id
            END
            WHERE bs.player_name = $1
            ORDER BY bs.points_scored DESC
            LIMIT 1
        """, name)

        # Owner who had them most
        top_owner = await db.fetchrow("""
            SELECT t.owner, t.team_id, COUNT(*) AS appearances
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            WHERE bs.player_name = $1
            GROUP BY t.owner, t.team_id
            ORDER BY appearances DESC
            LIMIT 1
        """, name)

        # Draft info
        draft_info = await db.fetchrow("""
            SELECT
                COUNT(*)                                AS times_drafted,
                ROUND(AVG(overall_pick)::numeric, 1)   AS avg_pick,
                MIN(overall_pick)                      AS earliest_pick,
                MIN(season)                            AS first_drafted
            FROM draft_picks
            WHERE player_name ILIKE $1
        """, name)

        # Seasons active
        seasons = await db.fetch("""
            SELECT DISTINCT season
            FROM box_scores
            WHERE player_name = $1
            ORDER BY season
        """, name)

        return {
            "player_name":   name,
            "position":      pos,
            "appearances":   player["appearances"],
            "avg_points":    float(player["avg_points"]),
            "best_game":     float(player["best_game"]),
            "worst_game":    float(player["worst_game"]),
            "total_points":  float(player["total_points"]),
            "seasons":       [r["season"] for r in seasons],
            "best_game_detail": {
                "points_scored": float(best_game_row["points_scored"]),
                "season":        best_game_row["season"],
                "week":          best_game_row["week"],
                "owner":         best_game_row["owner"],
                "opponent":      best_game_row["opponent"],
                "is_playoffs":   best_game_row["is_playoffs"],
            } if best_game_row else None,
            "top_owner": {
                "owner":       top_owner["owner"],
                "team_id":     top_owner["team_id"],
                "appearances": top_owner["appearances"],
            } if top_owner else None,
            "draft": {
                "times_drafted": draft_info["times_drafted"] or 0,
                "avg_pick":      float(draft_info["avg_pick"] or 0),
                "earliest_pick": draft_info["earliest_pick"],
                "first_drafted": draft_info["first_drafted"],
            } if draft_info else None,
        }


@router.get("/position/{position}/summary")
async def get_position_summary(position: str):
    """
    Aggregate stats for a position across all seasons and owners.
    Position: QB, RB, WR, TE, K, D/ST
    """
    async with (await get_pool()).acquire() as db:

        # Normalize position
        pos = position.upper().replace("-", "/")
        if pos == "DST":
            pos = "D/ST"

        # Overall scoring stats
        scoring = await db.fetchrow("""
            SELECT
                COUNT(*)                                        AS total_appearances,
                ROUND(AVG(points_scored)::numeric, 2)          AS avg_points,
                ROUND(MAX(points_scored)::numeric, 2)          AS max_points,
                ROUND(MIN(points_scored)::numeric, 2)          AS min_points,
                ROUND(STDDEV(points_scored)::numeric, 2)       AS stddev_points
            FROM box_scores
            WHERE position = $1
        """, pos)

        # Top 10 single-game performances
        top_games = await db.fetch("""
            SELECT
                bs.player_name,
                bs.points_scored,
                bs.season,
                bs.week,
                t.owner,
                t.team_id,
                m.is_playoffs,
                opp.owner AS opponent
            FROM box_scores bs
            JOIN teams t   ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            JOIN teams opp ON opp.team_id = CASE
                WHEN m.home_team_id = bs.team_id THEN m.away_team_id
                ELSE m.home_team_id
            END
            WHERE bs.position = $1
            ORDER BY bs.points_scored DESC
            LIMIT 10
        """, pos)

        # Bottom 5 single-game scores
        bottom_games = await db.fetch("""
            SELECT
                bs.player_name,
                bs.points_scored,
                bs.season,
                bs.week,
                t.owner,
                t.team_id,
                m.is_playoffs
            FROM box_scores bs
            JOIN teams t   ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.position = $1
            ORDER BY bs.points_scored ASC
            LIMIT 5
        """, pos)

        # Most appearances (most rostered players at this position)
        most_rostered = await db.fetch("""
            SELECT
                bs.player_name,
                COUNT(*)                                        AS appearances,
                ROUND(AVG(bs.points_scored)::numeric, 2)       AS avg_points,
                ROUND(SUM(bs.points_scored)::numeric, 1)       AS total_points,
                ROUND(MAX(bs.points_scored)::numeric, 2)       AS best_game
            FROM box_scores bs
            WHERE bs.position = $1
            GROUP BY bs.player_name
            ORDER BY appearances DESC
            LIMIT 10
        """, pos)

        # Avg points by season (trend over time)
        by_season = await db.fetch("""
            SELECT
                season,
                ROUND(AVG(points_scored)::numeric, 2)          AS avg_points,
                ROUND(MAX(points_scored)::numeric, 2)          AS max_points,
                COUNT(DISTINCT player_name)                    AS unique_players
            FROM box_scores
            WHERE position = $1
            GROUP BY season
            ORDER BY season
        """, pos)

        # Draft stats for this position
        draft_stats = await db.fetchrow("""
            SELECT
                COUNT(*)                                        AS times_drafted,
                ROUND(AVG(overall_pick)::numeric, 1)           AS avg_pick,
                MIN(overall_pick)                              AS earliest_pick,
                MAX(overall_pick)                              AS latest_pick,
                SUM(CASE WHEN is_keeper THEN 1 ELSE 0 END)    AS keeper_count
            FROM draft_picks
            WHERE position = $1
        """, pos)

        # Draft ADP trend by season
        draft_by_season = await db.fetch("""
            SELECT
                season,
                ROUND(AVG(overall_pick)::numeric, 1)           AS avg_pick,
                MIN(overall_pick)                              AS earliest_pick,
                COUNT(*)                                       AS times_drafted
            FROM draft_picks
            WHERE position = $1
            GROUP BY season
            ORDER BY season
        """, pos)

        # Most drafted players at this position (draft history)
        most_drafted = await db.fetch("""
            SELECT
                player_name,
                COUNT(*)                                        AS times_drafted,
                ROUND(AVG(overall_pick)::numeric, 1)           AS avg_pick,
                MIN(overall_pick)                              AS earliest_pick,
                MIN(season)                                    AS first_season,
                MAX(season)                                    AS last_season,
                SUM(CASE WHEN is_keeper THEN 1 ELSE 0 END)    AS keeper_count
            FROM draft_picks
            WHERE position = $1
            GROUP BY player_name
            ORDER BY times_drafted DESC
            LIMIT 10
        """, pos)

        return {
            "position": pos,
            "scoring": {
                "total_appearances": scoring["total_appearances"],
                "avg_points":        float(scoring["avg_points"] or 0),
                "max_points":        float(scoring["max_points"] or 0),
                "min_points":        float(scoring["min_points"] or 0),
                "stddev_points":     float(scoring["stddev_points"] or 0),
            },
            "top_games": [
                {**dict(r), "points_scored": float(r["points_scored"])}
                for r in top_games
            ],
            "bottom_games": [
                {**dict(r), "points_scored": float(r["points_scored"])}
                for r in bottom_games
            ],
            "most_rostered": [
                {**dict(r), "avg_points": float(r["avg_points"] or 0),
                 "total_points": float(r["total_points"] or 0),
                 "best_game": float(r["best_game"] or 0)}
                for r in most_rostered
            ],
            "by_season": [
                {**dict(r), "avg_points": float(r["avg_points"] or 0),
                 "max_points": float(r["max_points"] or 0)}
                for r in by_season
            ],
            "draft": {
                "times_drafted": draft_stats["times_drafted"] or 0,
                "avg_pick":      float(draft_stats["avg_pick"] or 0),
                "earliest_pick": draft_stats["earliest_pick"],
                "latest_pick":   draft_stats["latest_pick"],
                "keeper_count":  draft_stats["keeper_count"] or 0,
            },
            "draft_by_season": [dict(r) for r in draft_by_season],
            "most_drafted": [
                {**dict(r), "avg_pick": float(r["avg_pick"] or 0)}
                for r in most_drafted
            ],
        }
