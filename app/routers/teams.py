from fastapi import APIRouter, HTTPException
from app.database import get_pool

router = APIRouter(
    prefix="/teams",
    tags=["teams"]
)


@router.get("/")
async def get_all_teams():
    """All owners with career summary stats."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                t.team_id,
                t.owner,
                COUNT(DISTINCT r.season)                            AS seasons_played,
                SUM(r.wins)                                         AS total_wins,
                SUM(r.losses)                                       AS total_losses,
                SUM(r.draws)                                        AS total_draws,
                SUM(r.points_for)                                   AS total_points_for,
                SUM(r.points_against)                               AS total_points_against,
                SUM(CASE WHEN r.championship THEN 1 ELSE 0 END)    AS championships,
                SUM(CASE WHEN r.sacko        THEN 1 ELSE 0 END)    AS sackos,
                SUM(CASE WHEN r.most_points  THEN 1 ELSE 0 END)    AS most_points_seasons,
                MIN(r.season)                                       AS first_season,
                MAX(r.season)                                       AS last_season
            FROM teams t
            LEFT JOIN records r ON t.team_id = r.team_id
            GROUP BY t.team_id, t.owner
            ORDER BY total_wins DESC NULLS LAST
        """)

        return [
            {
                "team_id":            row["team_id"],
                "owner":              row["owner"],
                "seasons_played":     row["seasons_played"],
                "total_wins":         row["total_wins"] or 0,
                "total_losses":       row["total_losses"] or 0,
                "total_draws":        row["total_draws"] or 0,
                "total_points_for":   float(row["total_points_for"] or 0),
                "total_points_against": float(row["total_points_against"] or 0),
                "championships":      row["championships"] or 0,
                "sackos":             row["sackos"] or 0,
                "most_points_seasons": row["most_points_seasons"] or 0,
                "first_season":       row["first_season"],
                "last_season":        row["last_season"],
                "win_pct":            round(
                    (row["total_wins"] or 0) /
                    max((row["total_wins"] or 0) + (row["total_losses"] or 0), 1),
                    3
                ),
            }
            for row in rows
        ]


@router.get("/{team_id}")
async def get_team(team_id: int):
    """Full owner history — season-by-season record, career stats, matchup record."""
    async with (await get_pool()).acquire() as db:

        # Basic team info
        team = await db.fetchrow(
            "SELECT team_id, owner FROM teams WHERE team_id = $1", team_id)
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")

        # Season-by-season records
        seasons = await db.fetch("""
            SELECT
                r.season,
                r.wins,
                r.losses,
                r.draws,
                r.points_for,
                r.points_against,
                r.final_standing,
                r.championship,
                r.sacko,
                r.most_points,
                (SELECT COUNT(*) FROM records WHERE season = r.season) AS teams_in_season
            FROM records r
            WHERE r.team_id = $1
            ORDER BY r.season
        """, team_id)

        # Career totals
        career = await db.fetchrow("""
            SELECT
                COUNT(*)                                            AS seasons_played,
                SUM(wins)                                           AS total_wins,
                SUM(losses)                                         AS total_losses,
                SUM(draws)                                         AS total_draws,
                SUM(points_for)                                     AS total_points_for,
                SUM(points_against)                                 AS total_points_against,
                SUM(CASE WHEN championship THEN 1 ELSE 0 END)      AS championships,
                SUM(CASE WHEN sacko        THEN 1 ELSE 0 END)      AS sackos,
                SUM(CASE WHEN most_points  THEN 1 ELSE 0 END)      AS most_points_seasons,
                ROUND(AVG(final_standing), 2)                       AS avg_standing
            FROM records
            WHERE team_id = $1
        """, team_id)

        # All-time matchup record (wins/losses vs every opponent)
        h2h = await db.fetch("""
            SELECT
                opp.team_id                                         AS opp_team_id,
                opp.owner                                           AS opponent,
                COUNT(*)                                            AS total_games,
                SUM(CASE WHEN m.winner_team_id = $1 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN m.winner_team_id != $1
                         AND m.winner_team_id IS NOT NULL
                         THEN 1 ELSE 0 END)                         AS losses,
                SUM(CASE WHEN m.winner_team_id IS NULL THEN 1 ELSE 0 END) AS ties,
                ROUND(AVG(
                    CASE WHEN m.home_team_id = $1 THEN m.home_score
                         ELSE m.away_score END
                ), 2)                                               AS avg_score_for,
                ROUND(AVG(
                    CASE WHEN m.home_team_id = $1 THEN m.away_score
                         ELSE m.home_score END
                ), 2)                                               AS avg_score_against
            FROM matchups m
            JOIN teams opp ON opp.team_id = CASE
                WHEN m.home_team_id = $1 THEN m.away_team_id
                ELSE m.home_team_id
            END
            WHERE m.home_team_id = $1 OR m.away_team_id = $1
            GROUP BY opp.team_id, opp.owner
            ORDER BY wins DESC
        """, team_id)

        # Best and worst single-week scores
        scoring = await db.fetchrow("""
            SELECT
                MAX(CASE WHEN home_team_id = $1 THEN home_score ELSE away_score END) AS highest_score,
                MIN(CASE WHEN home_team_id = $1 THEN home_score ELSE away_score END) AS lowest_score,
                ROUND(AVG(
                    CASE WHEN home_team_id = $1 THEN home_score ELSE away_score END
                ), 2)                                               AS avg_score
            FROM matchups
            WHERE home_team_id = $1 OR away_team_id = $1
        """, team_id)

        return {
            "team_id": team["team_id"],
            "owner":   team["owner"],
            "career": {
                "seasons_played":      career["seasons_played"],
                "total_wins":          career["total_wins"] or 0,
                "total_losses":        career["total_losses"] or 0,
                "total_draws":         career["total_draws"] or 0,
                "total_points_for":    float(career["total_points_for"] or 0),
                "total_points_against": float(career["total_points_against"] or 0),
                "championships":       career["championships"] or 0,
                "sackos":              career["sackos"] or 0,
                "most_points_seasons": career["most_points_seasons"] or 0,
                "avg_standing":        float(career["avg_standing"] or 0),
                "win_pct":             round(
                    (career["total_wins"] or 0) /
                    max((career["total_wins"] or 0) + (career["total_losses"] or 0), 1),
                    3
                ),
                "highest_score":       float(scoring["highest_score"] or 0),
                "lowest_score":        float(scoring["lowest_score"] or 0),
                "avg_score":           float(scoring["avg_score"] or 0),
            },
            "seasons": [
                {
                    "season":         row["season"],
                    "wins":           row["wins"],
                    "losses":         row["losses"],
                    "draws":          row["draws"],
                    "points_for":     float(row["points_for"]),
                    "points_against": float(row["points_against"]),
                    "final_standing": row["final_standing"],
                    "teams_in_season": row["teams_in_season"],
                    "championship":   row["championship"],
                    "sacko":          row["sacko"],
                    "most_points":    row["most_points"],
                }
                for row in seasons
            ],
            "h2h": [
                {
                    "opp_team_id":    row["opp_team_id"],
                    "opponent":       row["opponent"],
                    "total_games":    row["total_games"],
                    "wins":           row["wins"],
                    "losses":         row["losses"],
                    "ties":           row["ties"],
                    "avg_score_for":  float(row["avg_score_for"] or 0),
                    "avg_score_against": float(row["avg_score_against"] or 0),
                }
                for row in h2h
            ],
        }
