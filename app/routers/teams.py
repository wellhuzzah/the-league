from fastapi import APIRouter, HTTPException
from app.database import concurrently, get_pool
from app.params import top_n, without

router = APIRouter(
    prefix="/teams",
    tags=["teams"]
)


def win_pct(wins, losses, draws):
    """NFL win percentage: a tie counts as half a win and half a loss. 0 with no games."""
    games = wins + losses + draws
    return round((wins + 0.5 * draws) / games, 3) if games else 0.0


async def career_totals(db):
    """Career summary for every owner, most wins first. Serves /teams/ and the Records page's
    all-time standings (which drops owners with no finished season)."""
    rows = await db.fetch("""
        SELECT
            t.team_id,
            t.owner,
            COUNT(r.id)                                         AS seasons_played,
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
        ORDER BY total_wins DESC NULLS LAST, t.owner
    """)
    out = []
    for row in rows:
        wins, losses, draws = row["total_wins"] or 0, row["total_losses"] or 0, row["total_draws"] or 0
        points_for = float(row["total_points_for"] or 0)
        out.append({
            "team_id":              row["team_id"],
            "owner":                row["owner"],
            "seasons_played":       row["seasons_played"],
            "total_wins":           wins,
            "total_losses":         losses,
            "total_draws":          draws,
            "total_points_for":     points_for,
            "total_points_against": float(row["total_points_against"] or 0),
            "championships":        row["championships"] or 0,
            "sackos":               row["sackos"] or 0,
            "most_points_seasons":  row["most_points_seasons"] or 0,
            "first_season":         row["first_season"],
            "last_season":          row["last_season"],
            "win_pct":              win_pct(wins, losses, draws),
            "ppg":                  round(points_for / max(wins + losses + draws, 1), 2),
        })
    return out


@router.get("/")
async def get_all_teams():
    """All owners with career summary stats."""
    async with (await get_pool()).acquire() as db:
        return await career_totals(db)


@router.get("/{team_id}")
async def get_team(team_id: int):
    """Everything the Team page shows: season-by-season record, career stats, record against every
    opponent, best single-player games (20) and points by position per season."""
    team, seasons, career, h2h, scoring, best_weeks, position_totals = await concurrently(
        lambda db: db.fetchrow("SELECT team_id, owner FROM teams WHERE team_id = $1", team_id),
        lambda db: db.fetch("""
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
        """, team_id),
        lambda db: db.fetchrow("""
            SELECT
                COUNT(*)                                            AS seasons_played,
                MIN(season)                                         AS first_season,
                MAX(season)                                         AS last_season,
                SUM(wins)                                           AS total_wins,
                SUM(losses)                                         AS total_losses,
                SUM(draws)                                          AS total_draws,
                SUM(points_for)                                     AS total_points_for,
                SUM(points_against)                                 AS total_points_against,
                SUM(CASE WHEN championship THEN 1 ELSE 0 END)      AS championships,
                SUM(CASE WHEN sacko        THEN 1 ELSE 0 END)      AS sackos,
                SUM(CASE WHEN most_points  THEN 1 ELSE 0 END)      AS most_points_seasons,
                ROUND(AVG(final_standing), 2)                       AS avg_standing
            FROM records
            WHERE team_id = $1
        """, team_id),
        # All-time record against every opponent, toilet bowl games aside (lineups often go unset)
        lambda db: db.fetch("""
            SELECT
                opp.team_id                                         AS opp_team_id,
                opp.owner                                           AS opponent,
                COUNT(*)                                            AS total_games,
                COUNT(*) FILTER (WHERE tg.result = 'W')             AS wins,
                COUNT(*) FILTER (WHERE tg.result = 'L')             AS losses,
                COUNT(*) FILTER (WHERE tg.result = 'T')             AS ties,
                ROUND(AVG(tg.score), 2)                             AS avg_score_for,
                ROUND(AVG(tg.opp_score), 2)                         AS avg_score_against
            FROM team_games tg
            JOIN teams opp ON opp.team_id = tg.opp_id
            WHERE tg.team_id = $1 AND tg.playoff_tier IS DISTINCT FROM 'LOSERS_CONSOLATION_LADDER'
            GROUP BY opp.team_id, opp.owner
            ORDER BY wins DESC, opp.owner
        """, team_id),
        # Best and worst single-week scores
        lambda db: db.fetchrow("""
            SELECT MAX(score) AS highest_score, MIN(score) AS lowest_score,
                   ROUND(AVG(score), 2) AS avg_score
            FROM team_games
            WHERE team_id = $1
        """, team_id),
        # Best 20 single-player games, plus any tied with the 20th
        lambda db: db.fetch(top_n("""
            SELECT
                bs.season,
                bs.week,
                bs.player_name,
                bs.position,
                bs.points_scored,
                bs.is_starter,
                tg.is_playoffs,
                tg.playoff_tier,
                opp.owner AS opponent,
                bs.espn_player_id
            FROM box_scores bs
            JOIN team_games tg ON tg.matchup_id = bs.matchup_id AND tg.team_id = bs.team_id
            JOIN teams opp ON opp.team_id = tg.opp_id
            WHERE bs.team_id = $1
              AND bs.is_starter = TRUE
        """, "points_scored DESC", "points_scored DESC, season, week, espn_player_id", "20"), team_id),
        lambda db: db.fetch("""
            SELECT
                bs.season,
                bs.position,
                ROUND(SUM(bs.points_scored)::numeric, 1)  AS total_points,
                ROUND(AVG(bs.points_scored)::numeric, 2)  AS avg_points,
                COUNT(*)                                   AS appearances
            FROM box_scores bs
            JOIN matchups m ON bs.matchup_id = m.id
            WHERE bs.team_id = $1 AND bs.position != 'UNK'
              AND bs.is_starter = TRUE
              AND NOT m.is_playoffs
            GROUP BY bs.season, bs.position
            ORDER BY bs.season, total_points DESC, bs.position
        """, team_id),
    )
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    by_season = {}
    for row in position_totals:
        by_season.setdefault(row["season"], {"season": row["season"], "positions": []})["positions"].append({
            "position":     row["position"],
            "total_points": float(row["total_points"]),
            "avg_points":   float(row["avg_points"]),
            "appearances":  row["appearances"],
        })

    return {
        "team_id": team["team_id"],
        "owner":   team["owner"],
        "career": {
            "seasons_played":      career["seasons_played"],
            "first_season":        career["first_season"],
            "last_season":         career["last_season"],
            "total_wins":          career["total_wins"] or 0,
            "total_losses":        career["total_losses"] or 0,
            "total_draws":         career["total_draws"] or 0,
            "total_points_for":    float(career["total_points_for"] or 0),
            "total_points_against": float(career["total_points_against"] or 0),
            "championships":       career["championships"] or 0,
            "sackos":              career["sackos"] or 0,
            "most_points_seasons": career["most_points_seasons"] or 0,
            "avg_standing":        float(career["avg_standing"] or 0),
            "win_pct":             win_pct(career["total_wins"] or 0, career["total_losses"] or 0,
                                           career["total_draws"] or 0),
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
                "win_pct":        win_pct(row["wins"], row["losses"], row["draws"]),
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
        "best_weeks": [{**row, "points_scored": float(row["points_scored"])}
                       for row in without(best_weeks, "place", "espn_player_id")],
        "position_totals": list(by_season.values()),
    }
