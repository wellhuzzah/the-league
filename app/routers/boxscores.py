from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from app.database import concurrently, get_pool
from app.params import top_n, without

router = APIRouter(
    prefix="/boxscores",
    tags=["boxscores"]
)


@router.get("/week/{season}/{week}")
async def get_week_boxscores(season: int, week: int, include_bench: bool = False,
                             team_id: Optional[list[int]] = Query(None)):
    """Player scores for a given week, starters only unless include_bench. ?team_id= (repeatable)
    limits it to those teams, e.g. both sides of a head-to-head game."""
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
              AND (bs.is_starter = TRUE OR $3)
              AND ($4::int[] IS NULL OR bs.team_id = ANY($4::int[]))
            ORDER BY t.owner, bs.team_id, bs.points_scored DESC, bs.espn_player_id
        """, season, week, include_bench, team_id)

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


async def random_player_card(name, player):
    """The random-player card for a picked player (a row of the pick query)."""
    best_game_row, top_owner, draft_info, seasons = await concurrently(
        # Best single game details
        lambda db: db.fetchrow("""
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
              AND bs.is_starter = TRUE
            ORDER BY bs.points_scored DESC, bs.season, bs.week, bs.team_id, bs.espn_player_id
            LIMIT 1
        """, name),
        # Owner who had them most
        lambda db: db.fetchrow("""
            SELECT t.owner, t.team_id, COUNT(*) AS appearances
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            WHERE bs.player_name = $1
              AND bs.is_starter = TRUE
            GROUP BY t.owner, t.team_id
            ORDER BY appearances DESC, t.owner, t.team_id
            LIMIT 1
        """, name),
        lambda db: db.fetchrow("""
            SELECT
                COUNT(*)                                AS times_drafted,
                ROUND(AVG(overall_pick)::numeric, 1)   AS avg_pick,
                MIN(overall_pick)                      AS earliest_pick,
                MIN(season)                            AS first_drafted
            FROM draft_picks
            WHERE lower(player_name) = lower($1)
        """, name),
        lambda db: db.fetch("""
            SELECT DISTINCT season
            FROM box_scores
            WHERE player_name = $1
              AND is_starter = TRUE
            ORDER BY season
        """, name),
    )
    return {
        "player_name":   name,
        "position":      player["position"],
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


@router.get("/random-player")
async def get_random_player():
    """A random player (5+ starts) with highlight stats."""
    async with (await get_pool()).acquire() as db:
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
              AND is_starter = TRUE
            GROUP BY player_name, position
            HAVING COUNT(*) >= 5
            ORDER BY RANDOM()
            LIMIT 1
        """)
    if not player:
        return None
    return await random_player_card(player["player_name"], player)


@router.get("/position/{position:path}/summary")
async def get_position_summary(position: str):
    """
    Aggregate stats for a position across all seasons and owners.
    Position: QB, RB, WR, TE, K, D/ST
    """
    if not position.strip():
        raise HTTPException(status_code=404, detail="Position not found")

    # Normalize position
    pos = position.upper().replace("-", "/")
    if pos == "DST":
        pos = "D/ST"

    (scoring, top_games, bottom_games, most_rostered, by_season, draft_stats, draft_by_season,
     most_drafted) = await concurrently(
        # Overall scoring stats
        lambda db: db.fetchrow("""
            SELECT
                COUNT(*)                                        AS total_appearances,
                ROUND(AVG(bs.points_scored)::numeric, 2)       AS avg_points,
                ROUND(MAX(bs.points_scored)::numeric, 2)       AS max_points,
                ROUND(MIN(bs.points_scored)::numeric, 2)       AS min_points,
                ROUND(STDDEV(bs.points_scored)::numeric, 2)    AS stddev_points
            FROM box_scores bs
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.position = $1
              AND bs.is_starter = TRUE
              AND NOT m.is_playoffs
        """, pos),
        # Top 10 single-game performances (plus any tied with the 10th; same for the lists below)
        lambda db: db.fetch(top_n("""
            SELECT
                bs.player_name,
                bs.points_scored,
                bs.season,
                bs.week,
                t.owner,
                t.team_id,
                m.is_playoffs,
                opp.owner AS opponent,
                bs.espn_player_id
            FROM box_scores bs
            JOIN teams t   ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            JOIN teams opp ON opp.team_id = CASE
                WHEN m.home_team_id = bs.team_id THEN m.away_team_id
                ELSE m.home_team_id
            END
            WHERE bs.position = $1
              AND bs.is_starter = TRUE
        """, "points_scored DESC", "points_scored DESC, season, week, team_id, espn_player_id", "10"), pos),
        # Bottom 5 single-game scores
        lambda db: db.fetch(top_n("""
            SELECT
                bs.player_name,
                bs.points_scored,
                bs.season,
                bs.week,
                t.owner,
                t.team_id,
                m.is_playoffs,
                bs.espn_player_id
            FROM box_scores bs
            JOIN teams t   ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.position = $1
              AND bs.is_starter = TRUE
        """, "points_scored ASC", "points_scored ASC, season, week, team_id, espn_player_id", "5"), pos),
        # Most appearances (most rostered players at this position)
        lambda db: db.fetch(top_n("""
            SELECT
                bs.player_name,
                COUNT(*)                                        AS appearances,
                ROUND(AVG(bs.points_scored)::numeric, 2)       AS avg_points,
                ROUND(SUM(bs.points_scored)::numeric, 1)       AS total_points,
                ROUND(MAX(bs.points_scored)::numeric, 2)       AS best_game
            FROM box_scores bs
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.position = $1
              AND bs.is_starter = TRUE
              AND NOT m.is_playoffs
            GROUP BY bs.player_name
        """, "appearances DESC", "appearances DESC, player_name", "10"), pos),
        # Avg points by season (trend over time)
        lambda db: db.fetch("""
            SELECT
                bs.season,
                ROUND(AVG(bs.points_scored)::numeric, 2)       AS avg_points,
                ROUND(MAX(bs.points_scored)::numeric, 2)       AS max_points,
                COUNT(DISTINCT bs.player_name)                 AS unique_players
            FROM box_scores bs
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.position = $1
              AND bs.is_starter = TRUE
              AND NOT m.is_playoffs
            GROUP BY bs.season
            ORDER BY bs.season
        """, pos),
        # Draft stats for this position
        lambda db: db.fetchrow("""
            SELECT
                COUNT(*)                                        AS times_drafted,
                ROUND(AVG(overall_pick)::numeric, 1)           AS avg_pick,
                MIN(overall_pick)                              AS earliest_pick,
                MAX(overall_pick)                              AS latest_pick,
                SUM(CASE WHEN is_keeper THEN 1 ELSE 0 END)    AS keeper_count
            FROM draft_picks
            WHERE position = $1
        """, pos),
        # Draft ADP trend by season
        lambda db: db.fetch("""
            SELECT
                season,
                ROUND(AVG(overall_pick)::numeric, 1)           AS avg_pick,
                MIN(overall_pick)                              AS earliest_pick,
                COUNT(*)                                       AS times_drafted
            FROM draft_picks
            WHERE position = $1
            GROUP BY season
            ORDER BY season
        """, pos),
        # Most drafted players at this position (draft history)
        lambda db: db.fetch(top_n("""
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
        """, "times_drafted DESC", "times_drafted DESC, player_name", "10"), pos),
    )
    top_games, bottom_games = without(top_games, "place", "espn_player_id"), without(bottom_games, "place", "espn_player_id")
    most_rostered, most_drafted = without(most_rostered), without(most_drafted)

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
