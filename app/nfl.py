"""NFL teams on player objects (tables from nfl_team_import.py; plan in docs/nfl_team_handoff.md).

Each helper takes an endpoint's finished rows (dicts with espn_player_id, and season / week where
needed) and sets two keys on every row, in place, with one batched query:
    nfl_team       abbreviation as of that season ("OAK", "LV"), or "OAK→DAL" for a season summary
    nfl_team_name  full name ("Oakland Raiders"), or "Oakland Raiders → Dallas Cowboys"
Both are None when the team isn't known; the site then shows nothing (it never guesses).

    week_teams     the team in that week (box scores, transactions; week 0, the preseason, = Week 1)
    draft_teams    the team in Week 1 of the row's season (draft picks: the team he was drafted from)
    season_teams   every team that season in week order, consecutive repeats collapsed (season summaries)
    current_teams  the team now (players table)
"""

from app.database import get_pool

FULL = "nt.location || ' ' || nt.name"


def _set(rows, found, key):
    for r in rows:
        team = found.get(key(r))
        r["nfl_team"], r["nfl_team_name"] = team if team else (None, None)
    return rows


async def _fetch(db, sql, *args):
    if db is not None:
        return await db.fetch(sql, *args)
    async with (await get_pool()).acquire() as conn:
        return await conn.fetch(sql, *args)


async def week_teams(rows, db=None, season=None, week=None):
    """Team in each row's week. season / week override the row's own (e.g. the whole response is
    one week)."""
    def key(r):
        return (r["espn_player_id"], season or r["season"], max(week or r["week"] or 1, 1))
    keys = list({key(r) for r in rows})
    if not keys:
        return rows
    found = await _fetch(db, f"""
        SELECT w.espn_player_id, w.season, w.week, nt.abbrev, {FULL} AS full_name
        FROM unnest($1::int[], $2::int[], $3::int[]) k(pid, season, week)
        JOIN player_team_weeks w ON w.espn_player_id = k.pid AND w.season = k.season AND w.week = k.week
        JOIN nfl_teams nt ON nt.season = w.season AND nt.pro_team_id = w.pro_team_id
    """, [k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys])
    return _set(rows, {(f["espn_player_id"], f["season"], f["week"]): (f["abbrev"], f["full_name"]) for f in found}, key)


async def draft_teams(rows, db=None, season=None):
    """Team in Week 1 of each row's season (the team at the draft)."""
    return await week_teams(rows, db, season=season, week=1)


async def season_teams(rows, db=None, season=None):
    """Every team the player had that season, in week order: "SF", or "OAK→DAL"."""
    def key(r):
        return (r["espn_player_id"], season or r["season"])
    keys = list({key(r) for r in rows})
    if not keys:
        return rows
    found = await _fetch(db, f"""
        SELECT w.espn_player_id, w.season, w.week, nt.abbrev, {FULL} AS full_name
        FROM unnest($1::int[], $2::int[]) k(pid, season)
        JOIN player_team_weeks w ON w.espn_player_id = k.pid AND w.season = k.season
        JOIN nfl_teams nt ON nt.season = w.season AND nt.pro_team_id = w.pro_team_id
        ORDER BY w.espn_player_id, w.season, w.week
    """, [k[0] for k in keys], [k[1] for k in keys])
    seq = {}
    for f in found:
        teams = seq.setdefault((f["espn_player_id"], f["season"]), [])
        if not teams or teams[-1][0] != f["abbrev"]:
            teams.append((f["abbrev"], f["full_name"]))
    summary = {k: ("→".join(a for a, _ in t), " → ".join(n for _, n in t)) for k, t in seq.items()}
    return _set(rows, summary, key)


async def current_teams(rows, db=None):
    """The team now (None: no team, retired, or not in ESPN's current player pool)."""
    def key(r):
        return r["espn_player_id"]
    pids = list({key(r) for r in rows})
    if not pids:
        return rows
    found = await _fetch(db, f"""
        SELECT p.espn_player_id, nt.abbrev, {FULL} AS full_name
        FROM players p
        JOIN nfl_teams nt ON nt.season = p.team_season AND nt.pro_team_id = p.pro_team_id
        WHERE p.espn_player_id = ANY($1::int[])
    """, pids)
    return _set(rows, {f["espn_player_id"]: (f["abbrev"], f["full_name"]) for f in found}, key)
