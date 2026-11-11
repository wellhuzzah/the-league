from fastapi import APIRouter
from app.database import concurrently, get_pool

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
                m.playoff_tier,
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

        # Compute summary (toilet bowl games aside)
        record   = counted(games)
        a_wins   = sum(1 for g in record if g["winner"] == "A")
        b_wins   = sum(1 for g in record if g["winner"] == "B")
        ties     = sum(1 for g in record if g["winner"] == "TIE")
        reg      = [g for g in record if not g["is_playoffs"]]
        playoffs = [g for g in record if is_playoff_game(g)]

        return {
            "team_a_id":    team_id_a,
            "team_b_id":    team_id_b,
            "total_games":  len(record),
            "toilet_games": len(games) - len(record),
            "team_a_wins":  a_wins,
            "team_b_wins":  b_wins,
            "ties":         ties,
            "regular_season_games":  len(reg),
            "playoff_games":         len(playoffs),
            "games":        games
        }


# Playoff games: the winners bracket and its consolation ladder (3rd/5th/7th place). is_playoffs is
# every game from week 14 on, so it also covers the losers' ladder, the toilet bowl.
PLAYOFF_TIERS = ("WINNERS_BRACKET", "WINNERS_CONSOLATION_LADDER")
TOILET_TIER = "LOSERS_CONSOLATION_LADDER"


def is_playoff_game(game):
    return game["playoff_tier"] in PLAYOFF_TIERS


def counted(games):
    """The games a head-to-head record counts: all but the toilet bowl, where lineups often go
    unset. Toilet games stay in the game log (tagged) but not in the record, averages or highlights."""
    return [g for g in games if g["playoff_tier"] != TOILET_TIER]


async def pair_games(a_id, b_id):
    """All games between two teams and both owners' names, fetched at once."""
    games, owners = await concurrently(
        lambda db: db.fetch("""
            SELECT m.season, m.week, m.is_playoffs, m.playoff_tier, m.home_score, m.away_score, m.margin,
                   m.winner_team_id, m.home_team_id, m.away_team_id
            FROM matchups m
            WHERE (m.home_team_id = $1 AND m.away_team_id = $2)
               OR (m.home_team_id = $2 AND m.away_team_id = $1)
            ORDER BY m.season, m.week
        """, a_id, b_id),
        lambda db: db.fetch("SELECT team_id, owner FROM teams WHERE team_id IN ($1, $2)", a_id, b_id),
    )
    return games, {r["team_id"]: r["owner"] for r in owners}


def h2h_highlights(a_id, b_id, games, owner):
    """The random-h2h card for a pair: record, averages and four highlight games, toilet bowl
    games aside."""
    toilet_games = len(games)
    games = counted(games)
    toilet_games -= len(games)
    a_owner, b_owner = owner[a_id], owner[b_id]
    a_wins     = sum(1 for g in games if g["winner_team_id"] == a_id)
    b_wins     = sum(1 for g in games if g["winner_team_id"] == b_id)
    ties       = sum(1 for g in games if g["winner_team_id"] is None)
    playoff_games = [g for g in games if is_playoff_game(g)]

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
        "team_a": { "team_id": a_id, "owner": a_owner },
        "team_b": { "team_id": b_id, "owner": b_owner },
        "summary": {
            "total_games":    len(games),
            "team_a_wins":    a_wins,
            "team_b_wins":    b_wins,
            "ties":           ties,
            "playoff_games":  len(playoff_games),
            "toilet_games":   toilet_games,
            "team_a_avg":     round(a_avg, 2),
            "team_b_avg":     round(b_avg, 2),
        },
        "highlights": {
            "biggest_margin": {
                "season":  biggest["season"],
                "week":    biggest["week"],
                "margin":  float(biggest["margin"]),
                "winner":  a_owner if biggest["winner_team_id"] == a_id else b_owner,
                "loser":   b_owner if biggest["winner_team_id"] == a_id else a_owner,
                "winner_score": team_score(biggest, biggest["winner_team_id"]),
                "loser_score":  team_score(biggest, b_id if biggest["winner_team_id"] == a_id else a_id),
                "is_playoffs": biggest["is_playoffs"],
                "playoff_tier": biggest["playoff_tier"],
            },
            "closest_game": {
                "season":  closest["season"],
                "week":    closest["week"],
                "margin":  float(closest["margin"]),
                "winner":  a_owner if closest["winner_team_id"] == a_id else b_owner,
                "loser":   b_owner if closest["winner_team_id"] == a_id else a_owner,
                "winner_score": team_score(closest, closest["winner_team_id"]) if closest["winner_team_id"] else float(closest["home_score"]),
                "loser_score":  team_score(closest, b_id if closest["winner_team_id"] == a_id else a_id) if closest["winner_team_id"] else float(closest["away_score"]),
                "is_playoffs": closest["is_playoffs"],
                "playoff_tier": closest["playoff_tier"],
            },
            "team_a_best": {
                "score":  team_score(a_best, a_id),
                "season": a_best["season"],
                "week":   a_best["week"],
                "is_playoffs": a_best["is_playoffs"],
                "playoff_tier": a_best["playoff_tier"],
            },
            "team_b_best": {
                "score":  team_score(b_best, b_id),
                "season": b_best["season"],
                "week":   b_best["week"],
                "is_playoffs": b_best["is_playoffs"],
                "playoff_tier": b_best["playoff_tier"],
            },
        }
    }


@router.get("/random-h2h")
async def get_random_h2h():
    """A random head-to-head pairing with at least 5 games played (toilet bowl aside), with a
    highlight summary."""
    async with (await get_pool()).acquire() as db:
        pair = await db.fetchrow("""
            SELECT
                LEAST(home_team_id, away_team_id)    AS team_a_id,
                GREATEST(home_team_id, away_team_id) AS team_b_id,
                COUNT(*)                             AS total_games
            FROM matchups
            WHERE playoff_tier IS DISTINCT FROM 'LOSERS_CONSOLATION_LADDER'
            GROUP BY
                LEAST(home_team_id, away_team_id),
                GREATEST(home_team_id, away_team_id)
            HAVING COUNT(*) >= 5
            ORDER BY RANDOM()
            LIMIT 1
        """)
    if not pair:
        return None
    a_id, b_id = pair["team_a_id"], pair["team_b_id"]
    games, owner = await pair_games(a_id, b_id)
    return h2h_highlights(a_id, b_id, games, owner)
