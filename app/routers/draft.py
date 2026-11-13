from collections import defaultdict
from fastapi import APIRouter, HTTPException
from app.database import get_pool
from app.params import Limit

router = APIRouter(
    prefix="/draft",
    tags=["draft"]
)

# Every pick with its drafted player's regular-season starter points that season.
# By id, not name: names collide (2010 has two drafted Mike Williamses).
# Value (steals/busts) only means something for seasons with box scores (2018 on): before that every
# pick has 0 points, so those seasons get no ranking.
SCORED_SEASONS = "SELECT DISTINCT season FROM box_scores"

# Positions that can't be a steal. A last-round kicker who edges out the other kickers still jumps
# ~150 rank spots, but the best kicker is worth ~0.6 ppg over a replacement (about one game every
# seven seasons). Defenses are streamed: one team rarely keeps a D/ST all season, so a drafted one
# outscoring its slot isn't a steal either. Both still count in scoring_rank, so other picks' value
# is unchanged. The Draft page's season Steals table applies the same exclusion (rockwood-ui Draft.jsx).
NOT_STEALS = {"K", "D/ST"}


def can_steal(pick):
    return pick["season_points"] > 0 and pick["position"] not in NOT_STEALS
PICK_VALUE = """
    SELECT
        dp.season,
        dp.espn_player_id,
        dp.player_name,
        dp.position,
        dp.overall_pick,
        dp.round_num,
        dp.pick_in_round,
        dp.is_keeper,
        t.owner,
        t.team_id,
        COALESCE(ROUND(SUM(bs.points_scored)::numeric, 1), 0) AS season_points,
        COUNT(bs.week) AS weeks_active
    FROM draft_picks dp
    JOIN teams t ON dp.team_id = t.team_id
    LEFT JOIN (box_scores bs
               JOIN matchups bm ON bm.id = bs.matchup_id AND NOT bm.is_playoffs)
        ON bs.espn_player_id = dp.espn_player_id
       AND bs.season = dp.season
       AND bs.is_starter = TRUE
    {where}
    GROUP BY dp.season, dp.espn_player_id, dp.player_name, dp.position, dp.overall_pick,
             dp.round_num, dp.pick_in_round, dp.is_keeper, t.owner, t.team_id
    ORDER BY dp.season, dp.overall_pick
"""


def value_pick(row, with_season):
    pick = {"season": row["season"]} if with_season else {}
    pick.update({
        "espn_player_id": row["espn_player_id"],
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
    return pick


def rank_season(picks):
    """Add scoring_rank (by season points; unscored picks rank last) and value_gap (pick minus
    rank) to one season's picks, in place."""
    scored = sorted([p for p in picks if p["season_points"] > 0],
                    key=lambda p: p["season_points"], reverse=True)
    rank_map = {p["overall_pick"]: i + 1 for i, p in enumerate(scored)}
    for p in picks:
        p["scoring_rank"] = rank_map.get(p["overall_pick"], len(picks) + 1)
        p["value_gap"] = p["overall_pick"] - p["scoring_rank"]


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


async def draft_tendencies(db):
    """Round-by-round position tendency per owner across all seasons (Records page)."""
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


@router.get("/value/history")
async def get_value_history(limit: int = Limit(25)):
    """All-time best steals and busts across every draft season with box scores (2018 on)."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch(PICK_VALUE.format(where=f"WHERE dp.season IN ({SCORED_SEASONS})"))

    by_season = defaultdict(list)
    for row in rows:
        by_season[row["season"]].append(value_pick(row, with_season=True))

    all_picks = []
    for season_picks in by_season.values():
        rank_season(season_picks)
        all_picks.extend(season_picks)

    steals = with_ties(sorted(
        [p for p in all_picks if can_steal(p)],
        key=lambda p: p["value_gap"], reverse=True
    ), limit, "value_gap")

    busts = with_ties(sorted(
        [p for p in all_picks if p["round_num"] <= 2],
        key=lambda p: p["season_points"]
    ), limit, "season_points")

    return {"steals": steals, "busts": busts}


def with_ties(ranked, limit, key):
    """The first `limit` items of a ranked list plus every later item tied with the last one on key
    (league rule: equal values share a rank)."""
    if len(ranked) <= limit:
        return ranked
    cutoff = ranked[limit - 1][key]
    return ranked[:limit] + [p for p in ranked[limit:] if p[key] == cutoff]


@router.get("/{year}")
async def get_draft(year: int):
    """Everything the Draft page shows for a season, from one query: the board by round, and the
    value table (draft position against season scoring) with the steal and the bust. The page
    groups picks by team itself. A season without box scores (before 2018) has no value ranking:
    value.scoring is false and its picks, steal and bust are empty."""
    async with (await get_pool()).acquire() as db:
        rows = await db.fetch(PICK_VALUE.format(where="WHERE dp.season = $1"), year)
        has_scoring = await db.fetchval("SELECT EXISTS (SELECT 1 FROM box_scores WHERE season = $1)", year)
    if not rows:
        raise HTTPException(status_code=404, detail="Draft not found")

    rounds = {}
    for row in rows:
        rounds.setdefault(row["round_num"], []).append({
            "overall_pick":  row["overall_pick"],
            "pick_in_round": row["pick_in_round"],
            "espn_player_id": row["espn_player_id"],
            "player_name":   row["player_name"],
            "position":      row["position"],
            "is_keeper":     row["is_keeper"],
            "owner":         row["owner"],
            "team_id":       row["team_id"],
        })

    picks, steal, bust = [], None, None
    if has_scoring:
        picks = [value_pick(row, with_season=False) for row in rows]
        rank_season(picks)
        candidates = [p for p in picks if can_steal(p)]
        steal = max(candidates, key=lambda p: p["value_gap"]) if candidates else None
        early = [p for p in picks if p["round_num"] <= 2]
        bust  = min(early, key=lambda p: p["season_points"]) if early else None

    return {
        "season": year,
        "rounds": [{"round": r, "picks": p} for r, p in sorted(rounds.items())],
        "value": {
            "season":  year,
            "scoring": has_scoring,
            "picks":   sorted(picks, key=lambda p: p["value_gap"], reverse=True),
            "steal":   steal,
            "bust":    bust,
        },
    }
