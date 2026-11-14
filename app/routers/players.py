from fastapi import APIRouter, HTTPException, Query
from app.database import concurrently, get_pool
from app.nfl import current_teams
from app.params import like_escape, search_term
from app.routers.transactions import player_transactions

router = APIRouter(
    prefix="/players",
    tags=["players"]
)

SEARCH_LIMIT = 20

# NFL teams (player_team_weeks, players, nfl_teams; nfl_team_import.py). No row means unknown:
# nfl_team and nfl_team_name are then NULL and the site shows nothing.
NFL_NAME = "nt.location || ' ' || nt.name"

# A player's most recent name anywhere (ESPN renames: "Mark Ingram" -> "Mark Ingram II"), from box
# scores, draft picks and transactions; {pid} is the SQL expression for the player id.
LATEST_NAME = """(
    SELECT x.player_name FROM (
        SELECT player_name, season, week, 1 AS src FROM box_scores WHERE espn_player_id = {pid}
        UNION ALL
        SELECT player_name, season, 0, 0 FROM draft_picks WHERE espn_player_id = {pid}
        UNION ALL
        SELECT i.player_name, t.season, t.week, 0
        FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id
        WHERE i.espn_player_id = {pid} AND i.player_name IS NOT NULL
    ) x
    ORDER BY x.season DESC, x.week DESC, x.src DESC
    LIMIT 1
)"""


@router.get("/search")
async def search_players(q: str = Query(...)):
    """Players whose name contains q (2+ characters; % and _ match literally), one row per ESPN
    player id, at most 20. Order: by each player's best starting game (points, then date), then
    players who were only drafted, in draft order. appearances = starts whose name matched,
    drafted = picks whose name matched; position comes from the first of those; seasons = every
    season of those starts and picks, ascending (tells same-name players apart). The name shown is
    the most recent of the player's names that matched (so a search for "Washington" shows
    "Washington D/ST", not the team's later "Commanders D/ST")."""
    term = search_term(q)
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch("""
            WITH box AS (
                SELECT bs.espn_player_id, bs.player_name, bs.position, bs.season, bs.week,
                       ROW_NUMBER() OVER (ORDER BY bs.points_scored DESC, bs.season, bs.week,
                                                   bs.team_id, bs.espn_player_id) AS ord
                FROM box_scores bs
                JOIN teams t ON bs.team_id = t.team_id
                JOIN matchups m ON bs.matchup_id = m.id
                WHERE bs.player_name ILIKE $1
                  AND bs.is_starter = TRUE
            ),
            draft AS (
                SELECT dp.espn_player_id, dp.player_name, dp.position, dp.season,
                       ROW_NUMBER() OVER (ORDER BY dp.season, dp.overall_pick) AS ord
                FROM draft_picks dp
                JOIN teams t ON dp.team_id = t.team_id
                WHERE dp.player_name ILIKE $1
            ),
            latest_name AS (
                SELECT DISTINCT ON (espn_player_id) espn_player_id, player_name
                FROM (SELECT espn_player_id, player_name, season, week, 1 AS src FROM box
                      UNION ALL
                      SELECT espn_player_id, player_name, season, 0, 0 FROM draft) n
                ORDER BY espn_player_id, season DESC, week DESC, src DESC
            ),
            b AS (
                SELECT DISTINCT ON (espn_player_id) espn_player_id, player_name, position, ord,
                       COUNT(*) OVER (PARTITION BY espn_player_id) AS appearances
                FROM box ORDER BY espn_player_id, ord
            ),
            d AS (
                SELECT DISTINCT ON (espn_player_id) espn_player_id, player_name, position, ord,
                       COUNT(*) OVER (PARTITION BY espn_player_id) AS drafted
                FROM draft ORDER BY espn_player_id, ord
            ),
            found AS (
                SELECT COALESCE(b.espn_player_id, d.espn_player_id)    AS espn_player_id,
                       COALESCE(NULLIF(b.position, ''), NULLIF(d.position, ''), '?') AS position,
                       COALESCE(b.appearances, 0)                      AS appearances,
                       COALESCE(d.drafted, 0)                          AS drafted,
                       b.ord AS box_ord, d.ord AS draft_ord
                FROM b
                FULL JOIN d ON d.espn_player_id = b.espn_player_id
                ORDER BY b.ord NULLS LAST, d.ord
                LIMIT $2
            ),
            yrs AS (
                SELECT espn_player_id, array_agg(DISTINCT season ORDER BY season) AS seasons
                FROM (SELECT espn_player_id, season FROM box
                      UNION ALL
                      SELECT espn_player_id, season FROM draft) s
                GROUP BY espn_player_id
            )
            SELECT f.espn_player_id, n.player_name, f.position, f.appearances, f.drafted, y.seasons
            FROM found f
            JOIN latest_name n ON n.espn_player_id = f.espn_player_id
            JOIN yrs y ON y.espn_player_id = f.espn_player_id
            ORDER BY f.box_ord NULLS LAST, f.draft_ord
        """, f"%{like_escape(term)}%", SEARCH_LIMIT)
    # each with the player's NFL team now (app/nfl.py)
    return {"query": q, "players": await current_teams([dict(r) for r in rows])}


def box_history(rows, label):
    """A player's starts, best first, with totals per owner (same shape as the old
    /boxscores/player/{name}/history)."""
    if not rows:
        return {"query": label, "results": [], "total_appearances": 0}
    owner_totals = {}
    for row in rows:
        o = row["owner"]
        if o not in owner_totals:
            owner_totals[o] = {"owner": o, "team_id": row["team_id"], "appearances": 0, "total_points": 0.0}
        owner_totals[o]["appearances"] += 1
        owner_totals[o]["total_points"] = round(owner_totals[o]["total_points"] + float(row["points_scored"]), 1)
    return {
        "query":             label,
        "total_appearances": len(rows),
        "by_owner":          sorted(owner_totals.values(), key=lambda x: x["total_points"], reverse=True),
        "results":           [{**dict(row), "points_scored": float(row["points_scored"])} for row in rows],
    }


def draft_history(rows, label):
    """A player's draft picks in draft order (the Draft page's Player Draft History shows these)."""
    if not rows:
        return {"query": label, "results": [], "times_drafted": 0}
    return {"query": label, "times_drafted": len(rows), "results": [dict(row) for row in rows]}


@router.get("/{espn_player_id}")
async def get_player(espn_player_id: int):
    """One player's card in one call: starts (box), draft picks and executed adds/drops, all by
    ESPN player id, so a renamed player is one card and two players who share a name are two.
    NFL teams: nfl_team / nfl_team_name at the top are the team now (NULL: none, or retired); each
    box row has the team that week, each draft row the team in Week 1 of that season (NULL:
    unknown)."""
    box, draft, transactions, names, now = await concurrently(
        lambda db: db.fetch("""
            SELECT
                bs.season,
                bs.week,
                bs.player_name,
                bs.position,
                bs.points_scored,
                bs.is_starter,
                t.owner,
                t.team_id,
                m.is_playoffs,
                m.playoff_tier,
                nt.abbrev AS nfl_team,
                """ + NFL_NAME + """ AS nfl_team_name
            FROM box_scores bs
            JOIN teams t ON bs.team_id = t.team_id
            JOIN matchups m ON bs.matchup_id = m.id
            LEFT JOIN player_team_weeks w
                   ON w.espn_player_id = bs.espn_player_id AND w.season = bs.season AND w.week = bs.week
            LEFT JOIN nfl_teams nt ON nt.season = w.season AND nt.pro_team_id = w.pro_team_id
            WHERE bs.espn_player_id = $1
              AND bs.is_starter = TRUE
            ORDER BY bs.points_scored DESC, bs.season, bs.week, bs.team_id, bs.espn_player_id
        """, espn_player_id),
        lambda db: db.fetch("""
            SELECT
                dp.season,
                dp.overall_pick,
                dp.round_num,
                dp.pick_in_round,
                dp.player_name,
                dp.position,
                dp.is_keeper,
                t.owner,
                t.team_id,
                nt.abbrev AS nfl_team,
                """ + NFL_NAME + """ AS nfl_team_name
            FROM draft_picks dp
            JOIN teams t ON dp.team_id = t.team_id
            LEFT JOIN player_team_weeks w
                   ON w.espn_player_id = dp.espn_player_id AND w.season = dp.season AND w.week = 1
            LEFT JOIN nfl_teams nt ON nt.season = w.season AND nt.pro_team_id = w.pro_team_id
            WHERE dp.espn_player_id = $1
            ORDER BY dp.season, dp.overall_pick
        """, espn_player_id),
        lambda db: player_transactions(db, espn_player_id, None),
        lambda db: db.fetchrow(f"SELECT {LATEST_NAME.format(pid='$1')} AS player_name", espn_player_id),
        lambda db: db.fetchrow(f"""
            SELECT nt.abbrev AS nfl_team, {NFL_NAME} AS nfl_team_name
            FROM players p
            JOIN nfl_teams nt ON nt.season = p.team_season AND nt.pro_team_id = p.pro_team_id
            WHERE p.espn_player_id = $1""", espn_player_id),
    )
    label = names["player_name"]
    if label is None:
        raise HTTPException(status_code=404, detail="Player not found")
    transactions["query"] = label
    return {
        "espn_player_id": espn_player_id,
        "player_name":    label,
        "nfl_team":       now["nfl_team"] if now else None,
        "nfl_team_name":  now["nfl_team_name"] if now else None,
        "box":            box_history(box, label),
        "draft":          draft_history(draft, label),
        "transactions":   transactions,
    }
