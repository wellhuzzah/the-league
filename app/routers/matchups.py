from fastapi import APIRouter
from app.database import get_pool

router = APIRouter(
    prefix="/matchups",
    tags=["matchups"]
)


@router.get("/h2h/{team_id_a}/{team_id_b}")
async def get_head_to_head(team_id_a: int, team_id_b: int):
    """All-time head-to-head record between two owners across all seasons."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                m.home_score,
                m.away_score,
                m.margin,
                m.winner_team_id,
                m.home_team_id,
                m.away_team_id,
                t_home.owner AS home_owner,
                t_away.owner AS away_owner,
                CASE
                    WHEN m.home_team_id = $1 THEN m.home_score
                    ELSE m.away_score
                END AS team_a_score,
                CASE
                    WHEN m.home_team_id = $1 THEN m.away_score
                    ELSE m.home_score
                END AS team_b_score,
                CASE
                    WHEN m.winner_team_id = $1 THEN 'A'
                    WHEN m.winner_team_id = $2 THEN 'B'
                    ELSE 'TIE'
                END AS winner
            FROM matchups m
            JOIN teams t_home ON m.home_team_id = t_home.team_id
            JOIN teams t_away ON m.away_team_id = t_away.team_id
            WHERE
                (m.home_team_id = $1 AND m.away_team_id = $2)
                OR (m.home_team_id = $2 AND m.away_team_id = $1)
            ORDER BY m.season, m.week
        """, team_id_a, team_id_b)

        games = [dict(row) for row in rows]

        # Compute summary
        a_wins   = sum(1 for g in games if g["winner"] == "A")
        b_wins   = sum(1 for g in games if g["winner"] == "B")
        ties     = sum(1 for g in games if g["winner"] == "TIE")
        reg      = [g for g in games if not g["is_playoffs"]]
        playoffs = [g for g in games if g["is_playoffs"]]

        return {
            "team_a_id":    team_id_a,
            "team_b_id":    team_id_b,
            "total_games":  len(games),
            "team_a_wins":  a_wins,
            "team_b_wins":  b_wins,
            "ties":         ties,
            "regular_season_games":  len(reg),
            "playoff_games":         len(playoffs),
            "games":        games
        }


@router.get("/season/{year}")
async def get_season_matchups(year: int):
    """All matchups for a given season grouped by week."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.id,
                m.week,
                m.is_playoffs,
                m.home_team_id,
                m.away_team_id,
                m.home_score,
                m.away_score,
                m.margin,
                m.winner_team_id,
                t_home.owner AS home_owner,
                t_away.owner AS away_owner
            FROM matchups m
            JOIN teams t_home ON m.home_team_id = t_home.team_id
            JOIN teams t_away ON m.away_team_id = t_away.team_id
            WHERE m.season = $1
            ORDER BY m.week, m.id
        """, year)

        # Group by week
        weeks = {}
        for row in rows:
            w = row["week"]
            if w not in weeks:
                weeks[w] = {"week": w, "is_playoffs": row["is_playoffs"], "games": []}
            weeks[w]["games"].append({
                "id":            row["id"],
                "home_owner":    row["home_owner"],
                "away_owner":    row["away_owner"],
                "home_score":    float(row["home_score"]),
                "away_score":    float(row["away_score"]),
                "margin":        float(row["margin"]),
                "winner_team_id": row["winner_team_id"],
            })

        return {
            "season": year,
            "weeks":  sorted(weeks.values(), key=lambda x: x["week"])
        }


@router.get("/season/{year}/week/{week}")
async def get_week_matchups(year: int, week: int):
    """Single week results for a season."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.id,
                m.week,
                m.is_playoffs,
                m.home_team_id,
                m.away_team_id,
                m.home_score,
                m.away_score,
                m.margin,
                m.winner_team_id,
                t_home.owner AS home_owner,
                t_away.owner AS away_owner
            FROM matchups m
            JOIN teams t_home ON m.home_team_id = t_home.team_id
            JOIN teams t_away ON m.away_team_id = t_away.team_id
            WHERE m.season = $1 AND m.week = $2
            ORDER BY m.id
        """, year, week)

        return {
            "season":     year,
            "week":       week,
            "is_playoffs": rows[0]["is_playoffs"] if rows else False,
            "games": [
                {
                    "id":             row["id"],
                    "home_owner":     row["home_owner"],
                    "away_owner":     row["away_owner"],
                    "home_score":     float(row["home_score"]),
                    "away_score":     float(row["away_score"]),
                    "margin":         float(row["margin"]),
                    "winner_team_id": row["winner_team_id"],
                }
                for row in rows
            ]
        }


@router.get("/records/highest-scores")
async def get_highest_scores(limit: int = 10):
    """Highest single-week scores all time."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                t.owner,
                GREATEST(
                    CASE WHEN m.home_team_id = t.team_id THEN m.home_score ELSE m.away_score END,
                    0
                ) AS score,
                CASE WHEN m.home_team_id = t.team_id THEN m.away_score ELSE m.home_score END AS opp_score,
                t_opp.owner AS opponent
            FROM matchups m
            JOIN teams t ON t.team_id IN (m.home_team_id, m.away_team_id)
            JOIN teams t_opp ON t_opp.team_id = CASE
                WHEN m.home_team_id = t.team_id THEN m.away_team_id
                ELSE m.home_team_id
            END
            ORDER BY score DESC
            LIMIT $1
        """, limit)
        return [dict(row) for row in rows]


@router.get("/records/biggest-blowouts")
async def get_biggest_blowouts(limit: int = 10):
    """Games with the largest margin of victory all time."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                m.home_score,
                m.away_score,
                m.margin,
                t_home.owner AS home_owner,
                t_away.owner AS away_owner,
                t_winner.owner AS winner
            FROM matchups m
            JOIN teams t_home   ON m.home_team_id   = t_home.team_id
            JOIN teams t_away   ON m.away_team_id   = t_away.team_id
            LEFT JOIN teams t_winner ON m.winner_team_id = t_winner.team_id
            WHERE m.winner_team_id IS NOT NULL
              AND m.home_score > 0
              AND m.away_score > 0
            ORDER BY m.margin DESC
            LIMIT $1
        """, limit)
        return [
            {**dict(row), "margin": float(row["margin"]),
             "home_score": float(row["home_score"]),
             "away_score": float(row["away_score"])}
            for row in rows
        ]


@router.get("/records/closest-games")
async def get_closest_games(limit: int = 10):
    """Games with the smallest margin of victory all time."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                m.home_score,
                m.away_score,
                m.margin,
                t_home.owner AS home_owner,
                t_away.owner AS away_owner,
                t_winner.owner AS winner
            FROM matchups m
            JOIN teams t_home   ON m.home_team_id   = t_home.team_id
            JOIN teams t_away   ON m.away_team_id   = t_away.team_id
            LEFT JOIN teams t_winner ON m.winner_team_id = t_winner.team_id
            WHERE m.winner_team_id IS NOT NULL
            ORDER BY m.margin ASC
            LIMIT $1
        """, limit)
        return [
            {**dict(row), "margin": float(row["margin"]),
             "home_score": float(row["home_score"]),
             "away_score": float(row["away_score"])}
            for row in rows
        ]


@router.get("/random-h2h")
async def get_random_h2h():
    """
    Returns a random head-to-head pairing with at least 5 games played,
    along with a highlight summary.
    """
    async with (await get_pool()).acquire() as db:
        # Pick a random matchup pair with 5+ games
        pair = await db.fetchrow("""
            SELECT
                LEAST(home_team_id, away_team_id)    AS team_a_id,
                GREATEST(home_team_id, away_team_id) AS team_b_id,
                COUNT(*)                             AS total_games
            FROM matchups
            GROUP BY
                LEAST(home_team_id, away_team_id),
                GREATEST(home_team_id, away_team_id)
            HAVING COUNT(*) >= 5
            ORDER BY RANDOM()
            LIMIT 1
        """)

        if not pair:
            return None

        a_id = pair["team_a_id"]
        b_id = pair["team_b_id"]

        # Full game log between the two
        games = await db.fetch("""
            SELECT
                m.season,
                m.week,
                m.is_playoffs,
                m.home_score,
                m.away_score,
                m.margin,
                m.winner_team_id,
                m.home_team_id,
                m.away_team_id
            FROM matchups m
            WHERE
                (m.home_team_id = $1 AND m.away_team_id = $2)
                OR (m.home_team_id = $2 AND m.away_team_id = $1)
            ORDER BY m.season, m.week
        """, a_id, b_id)

        # Owner names
        team_a = await db.fetchrow("SELECT owner FROM teams WHERE team_id = $1", a_id)
        team_b = await db.fetchrow("SELECT owner FROM teams WHERE team_id = $1", b_id)

        # Compute summary
        a_wins     = sum(1 for g in games if g["winner_team_id"] == a_id)
        b_wins     = sum(1 for g in games if g["winner_team_id"] == b_id)
        ties       = sum(1 for g in games if g["winner_team_id"] is None)
        playoff_games = [g for g in games if g["is_playoffs"]]

        # Scores from each team's perspective
        def team_score(g, team_id):
            return float(g["home_score"] if g["home_team_id"] == team_id else g["away_score"])

        a_scores = [team_score(g, a_id) for g in games]
        b_scores = [team_score(g, b_id) for g in games]

        a_avg = sum(a_scores) / len(a_scores) if a_scores else 0
        b_avg = sum(b_scores) / len(b_scores) if b_scores else 0

        # Best game for each team
        a_best = max(games, key=lambda g: team_score(g, a_id))
        b_best = max(games, key=lambda g: team_score(g, b_id))

        # Biggest margin game
        biggest = max(games, key=lambda g: float(g["margin"]))
        closest = min(games, key=lambda g: float(g["margin"]))

        return {
            "team_a": { "team_id": a_id, "owner": team_a["owner"] },
            "team_b": { "team_id": b_id, "owner": team_b["owner"] },
            "summary": {
                "total_games":    len(games),
                "team_a_wins":    a_wins,
                "team_b_wins":    b_wins,
                "ties":           ties,
                "playoff_games":  len(playoff_games),
                "team_a_avg":     round(a_avg, 2),
                "team_b_avg":     round(b_avg, 2),
            },
            "highlights": {
                "biggest_margin": {
                    "season":  biggest["season"],
                    "week":    biggest["week"],
                    "margin":  float(biggest["margin"]),
                    "winner":  team_a["owner"] if biggest["winner_team_id"] == a_id else team_b["owner"],
                    "loser":   team_b["owner"] if biggest["winner_team_id"] == a_id else team_a["owner"],
                    "winner_score": team_score(biggest, biggest["winner_team_id"]),
                    "loser_score":  team_score(biggest, b_id if biggest["winner_team_id"] == a_id else a_id),
                    "is_playoffs": biggest["is_playoffs"],
                },
                "closest_game": {
                    "season":  closest["season"],
                    "week":    closest["week"],
                    "margin":  float(closest["margin"]),
                    "winner":  team_a["owner"] if closest["winner_team_id"] == a_id else team_b["owner"],
                    "loser":   team_b["owner"] if closest["winner_team_id"] == a_id else team_a["owner"],
                    "winner_score": team_score(closest, closest["winner_team_id"]) if closest["winner_team_id"] else float(closest["home_score"]),
                    "loser_score":  team_score(closest, b_id if closest["winner_team_id"] == a_id else a_id) if closest["winner_team_id"] else float(closest["away_score"]),
                    "is_playoffs": closest["is_playoffs"],
                },
                "team_a_best": {
                    "score":  team_score(a_best, a_id),
                    "season": a_best["season"],
                    "week":   a_best["week"],
                    "is_playoffs": a_best["is_playoffs"],
                },
                "team_b_best": {
                    "score":  team_score(b_best, b_id),
                    "season": b_best["season"],
                    "week":   b_best["week"],
                    "is_playoffs": b_best["is_playoffs"],
                },
            }
        }
