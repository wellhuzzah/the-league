#!/usr/bin/env python
"""
nfl_team_import.py - each player's NFL team: per week (player_team_weeks), the team list per season
(nfl_teams), and the team now (players). Reads the ESPN archive (espn_raw/) and nflverse's weekly
rosters; it never calls ESPN. Plan and evidence: docs/nfl_team_handoff.md.

Per (player, season, week), first match wins; anything else stays unknown (no row):
	espn      2018 on: ESPN's actual stat line for the week, when the player played. A line for a game
	          he didn't play can name a team he wasn't on yet (Amari Cooper 2018 Week 7), so those
	          lines are never used.
	nflverse  nflverse weekly roster with an accepted status (ACCEPTED_STATUS), exactly one team.
	          The only source before 2018. CC-BY 4.0, https://github.com/nflverse/nflverse-data
	gap_fill  no record that week (a bye, say), and the nearest recorded weeks on both sides agree.
	bye       no record that week, the two sides disagree or one is missing, and exactly one side's
	          team had no game in every week from there to this one (Amari Cooper 2018: Week 7 was
	          Oakland's bye, so OAK; Week 8 was Dallas's, so DAL). Byes come from nflverse: a team
	          with no roster rows in a week other teams have rows for had no game.
	dst_id    a D/ST: the team is -(espn_player_id) - 16000.
If nflverse contradicts a week ESPN shows him playing, nflverse is ignored for that player-season
(it can attach an ESPN id to the wrong person). ESPN ids nflverse gives to two people are dropped.
ESPN's player-level proTeamId is never used for a past week: 2018 on it's the team at download time,
and before 2018 it's the team the next season.

Players covered: everyone in that season's box_scores, draft_picks or transaction_items, for every
week of the season. players (the team now) comes from the newest archived kona_player_info of the
latest season, for every player the league has ever touched; ESPN's 0 (no team) is stored as NULL.

nflverse files are saved under espn_raw/nflverse/<file>/<UTC time>_<status>.csv.gz (never
overwritten). The latest season's roster file and players.csv are downloaded every run; other
seasons only when there's no copy (or with --refresh-nflverse). A failed download falls back to the
newest copy, with a warning.

	venv\\Scripts\\python.exe nfl_team_import.py --season 2026 --dry-run
	venv\\Scripts\\python.exe nfl_team_import.py --all              # backfill every season

Output ends with "=== SUMMARY ===" (JSON) and a WARNINGS block; weekly_maintenance.py reads both.
Exit 1 on anything it can't trust (unknown team abbreviation, missing archive file, no nflverse copy).
"""

import argparse
import asyncio
import csv
import glob
import gzip
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import asyncpg
import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from espn_raw import RAW_DIR, load_archived  # noqa: E402

FIRST_SEASON = 2009
ESPN_WEEKLY_FROM = 2018        # before this ESPN kept no weekly stat lines
NFLVERSE_DIR = os.path.join(RAW_DIR, "nflverse")
NFLVERSE_URL = "https://github.com/nflverse/nflverse-data/releases/download/{tag}/{name}.csv"
# Statuses that mean "on this team that week". DEV (practice squad) disagreed with ESPN on 781 of
# 1,755 checkable 2018 rows; CUT, RET, TRC/TRD/TRT and the rest mean he has left or is leaving.
ACCEPTED_STATUS = {"ACT", "RES", "INA", "PUP", "SUS", "NWT", "RSN", "EXE"}
NFLVERSE_TEAM = {
	"ARI": 22, "ARZ": 22, "ATL": 1, "BAL": 33, "BLT": 33, "BUF": 2, "CAR": 29, "CHI": 3, "CIN": 4,
	"CLE": 5, "CLV": 5, "DAL": 6, "DEN": 7, "DET": 8, "GB": 9, "HOU": 34, "HST": 34, "IND": 11,
	"JAX": 30, "JAC": 30, "KC": 12, "LA": 14, "LAR": 14, "STL": 14, "SL": 14, "LAC": 24, "SD": 24,
	"LV": 13, "OAK": 13, "MIA": 15, "MIN": 16, "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21,
	"PIT": 23, "SF": 25, "SEA": 26, "TB": 27, "TEN": 10, "WAS": 28, "WSH": 28,
}
TEAM_RESULT_STATS = {"155", "156", "157"}   # won / lost / tied: on a line even when he didn't play
DST_BASE = 16000
MIN_POOL = 1000                # a current-season pool smaller than this is a partial response


def final_week(season):
	return 17 if season <= 2020 else 18


def stamp():
	return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# --------------------------------------------------------------------------- #
# nfl_teams
# --------------------------------------------------------------------------- #
def newest(pattern):
	files = sorted(glob.glob(pattern))
	return files[-1] if files else None


def washington_name(season):
	return "Redskins" if season <= 2019 else "Football Team" if season <= 2021 else "Commanders"


def espn_team_list(season):
	"""{pro_team_id: (abbrev, location, name, bye)} from the newest archived proTeamSchedules_wl."""
	path = newest(os.path.join(RAW_DIR, str(season), "proTeamSchedules_wl", "*", "*_200.json.gz"))
	if not path:
		sys.exit(f"ERROR: no archived proTeamSchedules_wl for {season}")
	teams = {}
	for t in json.loads(load_archived(path))["settings"]["proTeams"]:
		if t["id"]:
			teams[t["id"]] = (t["abbrev"].upper(), t["location"], t["name"], t.get("byeWeek") or None)
	if len(teams) != 32:
		sys.exit(f"ERROR: {season} proTeamSchedules_wl lists {len(teams)} teams")
	return teams


def team_list(season):
	"""nfl_teams rows for a season: ESPN's list from 2018 on; before that, 2018's with the moves
	ESPN's ids hide. Washington's nickname is set by hand (ESPN shows none for 2019-20 and
	Commanders for 2021)."""
	if season >= ESPN_WEEKLY_FROM:
		teams = espn_team_list(season)
	else:
		teams = {i: (a, loc, n, None) for i, (a, loc, n, _) in espn_team_list(ESPN_WEEKLY_FROM).items()}
		teams[13] = ("OAK", "Oakland", "Raiders", None)
		teams[14] = ("STL", "St. Louis", "Rams", None) if season <= 2015 else ("LAR", "Los Angeles", "Rams", None)
		teams[24] = ("SD", "San Diego", "Chargers", None) if season <= 2016 else ("LAC", "Los Angeles", "Chargers", None)
	a, loc, _, bye = teams[28]
	teams[28] = (a, loc, washington_name(season), bye)
	return teams


# --------------------------------------------------------------------------- #
# nflverse
# --------------------------------------------------------------------------- #
def nflverse_file(name, tag, refresh, warnings):
	"""Text of an nflverse release file: downloaded (and saved) when refresh or no copy exists,
	else the newest saved copy. None when neither works."""
	folder = os.path.join(NFLVERSE_DIR, name)
	cached = newest(os.path.join(folder, "*_200.csv.gz"))
	if refresh or not cached:
		try:
			r = requests.get(NFLVERSE_URL.format(tag=tag, name=name), timeout=180,
			                 headers={"User-Agent": "rockwood-import/1.0"})
			os.makedirs(folder, exist_ok=True)
			with gzip.open(os.path.join(folder, f"{stamp()}_{r.status_code}.csv.gz"), "wb") as f:
				f.write(r.content)
			if r.status_code == 200:
				return r.content.decode("utf-8"), "downloaded"
			warnings.append(f"nflverse {name}: HTTP {r.status_code}" + (", using the saved copy" if cached else ""))
		except requests.RequestException as e:
			warnings.append(f"nflverse {name}: {type(e).__name__}" + (", using the saved copy" if cached else ""))
	if cached:
		return load_archived(cached).decode("utf-8"), "saved copy"
	return None, "unavailable"


def to_id(value):
	try:
		return int(float(value)) if value else None
	except ValueError:
		return None


def crosswalk(text):
	"""gsis_id -> espn id, from nflverse players.csv (for roster rows with no espn_id)."""
	return {r["gsis_id"]: to_id(r["espn_id"]) for r in csv.DictReader(io.StringIO(text))
	        if r.get("gsis_id") and to_id(r.get("espn_id"))}


PUBLISHED_TEAMS = 24           # a week counts as published once this many teams have roster rows


def nflverse_weeks(text, xwalk, final):
	"""({espn id: {week: {team ids}}}, {team: weeks it had no game}, counts) from regular-season rows.
	Teams come only from rows with an accepted status. A row with no espn_id goes through the
	crosswalk, unless that id already has rows of its own. An espn id that nflverse gives to two
	different people (gsis ids) is dropped. A team has a roster in every week it plays, so a
	published week (PUBLISHED_TEAMS) with no rows for a team is a week it had no game."""
	direct, walked = defaultdict(lambda: defaultdict(set)), defaultdict(lambda: defaultdict(set))
	people, rostered = defaultdict(set), defaultdict(set)
	counts = Counter()
	reader = csv.DictReader(io.StringIO(text))
	missing = {"season", "team", "status", "espn_id", "gsis_id", "week", "game_type"} - set(reader.fieldnames or [])
	if missing:
		sys.exit(f"ERROR: nflverse roster file lacks columns {sorted(missing)}")
	for r in reader:
		if r["game_type"] != "REG":
			continue
		team = NFLVERSE_TEAM.get(r["team"])
		if team is None:
			sys.exit(f"ERROR: unknown nflverse team abbreviation {r['team']!r}")
		week = int(r["week"])
		rostered[week].add(team)
		if r["status"] not in ACCEPTED_STATUS:
			continue
		eid = to_id(r["espn_id"])
		if eid:
			direct[eid][week].add(team)
			people[eid].add(r["gsis_id"] or r["full_name"])
		elif xwalk.get(r["gsis_id"]):
			walked[xwalk[r["gsis_id"]]][week].add(team)
			people[xwalk[r["gsis_id"]]].add(r["gsis_id"])
		else:
			counts["rows_without_espn_id"] += 1
	for eid, weeks in walked.items():
		if eid not in direct:
			direct[eid] = weeks
			counts["players_via_crosswalk"] += 1
	for eid in [e for e in direct if len(people[e]) > 1]:
		del direct[eid]
		counts["espn_ids_on_two_people"] += 1
	published = [w for w in range(1, final + 1) if len(rostered[w]) >= PUBLISHED_TEAMS]
	no_game = {t: {w for w in published if t not in rostered[w]} for t in NFLVERSE_TEAM.values()}
	return direct, no_game, counts


# --------------------------------------------------------------------------- #
# ESPN archive
# --------------------------------------------------------------------------- #
def player_objects(o):
	if isinstance(o, dict):
		if "fullName" in o and "id" in o and "stats" in o:
			yield o
		for v in o.values():
			yield from player_objects(v)
	elif isinstance(o, list):
		for v in o:
			yield from player_objects(v)


def espn_files(season):
	"""The newest response in every archived folder that carries players' weekly stat lines."""
	folders = (glob.glob(os.path.join(RAW_DIR, str(season), "mBoxscore*", "*"))
	           + glob.glob(os.path.join(RAW_DIR, str(season), "kona_player_info*", "*")))
	return [f for f in (newest(os.path.join(d, "*_200.json.gz")) for d in folders) if f]


def espn_played_weeks(season):
	"""({pid: {week: team}} from actual lines of weeks he played, [(pid, week, teams)] where two
	played lines name different teams; those weeks are left to the other sources)."""
	seen = defaultdict(lambda: defaultdict(set))
	for path in espn_files(season):
		for p in player_objects(json.loads(load_archived(path))):
			if p["id"] < 0:
				continue
			for st in p.get("stats") or []:
				week = st.get("scoringPeriodId")
				if (st.get("seasonId") == season and st.get("statSourceId") == 0 and st.get("statSplitTypeId") == 1
				        and st.get("proTeamId") and 1 <= (week or 0) <= final_week(season)
				        and any(k not in TEAM_RESULT_STATS for k in (st.get("stats") or {}))):
					seen[p["id"]][week].add(st["proTeamId"])
	out, split = defaultdict(dict), []
	for pid, weeks in seen.items():
		for week, teams in weeks.items():
			if len(teams) == 1:
				out[pid][week] = next(iter(teams))
			else:
				split.append((pid, week, sorted(teams)))
	return out, split


def current_pool(season):
	"""{pid: proTeamId or None} from the newest archived kona_player_info of the season."""
	path = newest(os.path.join(RAW_DIR, str(season), "kona_player_info", "none", "*_200.json.gz"))
	if not path:
		sys.exit(f"ERROR: no archived kona_player_info for {season}")
	pool = {p["id"]: (p.get("proTeamId") or None) for p in json.loads(load_archived(path)) if "id" in p}
	if len(pool) < MIN_POOL:
		sys.exit(f"ERROR: {season} kona_player_info has only {len(pool)} players")
	return pool, os.path.basename(path)


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def resolve(espn, nflv, weeks, no_game):
	"""{week: (team, source)} for one player-season, and the weeks where nflverse contradicts a played
	ESPN line. espn: {week: team}; nflv: {week: {teams}}; no_game: {team: weeks it had no game}.
	One contradiction means nflverse may have the wrong person (two Michael Carters in 2023), so
	nflverse is dropped for the whole player-season and ESPN stands alone."""
	conflicts = [(w, e, sorted(nflv[w])) for w, e in espn.items() if nflv.get(w) and e not in nflv[w]]
	if conflicts:
		nflv = {}
	known = {}
	for w in weeks:
		e, n = espn.get(w), nflv.get(w) or set()
		if e is not None:
			known[w] = (e, "espn")
		elif len(n) == 1:
			known[w] = (next(iter(n)), "nflverse")
	out, ks = dict(known), sorted(known)

	def idle(team, lo, hi):
		"""team had no game in any week lo..hi"""
		return all(x in no_game.get(team, ()) for x in range(lo, hi + 1))

	for w in weeks:
		if w in known:
			continue
		before, after = [k for k in ks if k < w], [k for k in ks if k > w]
		b = known[before[-1]][0] if before else None
		a = known[after[0]][0] if after else None
		if b is not None and b == a:
			out[w] = (b, "gap_fill")
			continue
		# a week the neighbouring team didn't play (a bye next to a trade, the season's edge)
		fits = {t for t, ok in ((b, b is not None and idle(b, before[-1] + 1, w)),
		                        (a, a is not None and idle(a, w, after[0] - 1))) if ok}
		if len(fits) == 1:
			out[w] = (fits.pop(), "bye")
	return out, conflicts


async def league_rows(conn, season):
	"""[(pid, week)] the league shows for a season: box score rows, transaction items (week 0, the
	preseason, counts as week 1) and draft picks (Week 1)."""
	rows = await conn.fetch("""
		SELECT espn_player_id pid, week FROM box_scores WHERE season = $1
		UNION ALL
		SELECT i.espn_player_id, GREATEST(t.week, 1) FROM transaction_items i
		JOIN transactions t ON t.id = i.transaction_id WHERE t.season = $1
		UNION ALL
		SELECT espn_player_id, 1 FROM draft_picks WHERE season = $1""", season)
	return [(r["pid"], r["week"]) for r in rows]


async def build_season(conn, season, latest, refresh, xwalk, warnings, report):
	teams = team_list(season)
	text, how = nflverse_file(f"roster_weekly_{season}", "weekly_rosters", refresh or season == latest, warnings)
	if text is None:
		sys.exit(f"ERROR: no nflverse roster file for {season}")
	nflv, no_game, ncounts = nflverse_weeks(text, xwalk, final_week(season))
	espn, split = espn_played_weeks(season) if season >= ESPN_WEEKLY_FROM else ({}, [])

	league_rows_list = await league_rows(conn, season)
	pids = sorted({pid for pid, _ in league_rows_list})
	# the season so far: past seasons run to the final week; the current one to the last week with data
	last = final_week(season)
	if season == latest:
		seen = [w for p in espn.values() for w in p] + [w for p in nflv.values() for w in p]
		last = min(last, max(seen, default=0))
	weeks = range(1, last + 1)

	rows, conflicts, dst_bad = {}, [], []
	for pid in pids:
		if pid < 0:
			team = -pid - DST_BASE
			if team not in teams:
				dst_bad.append(pid)
				continue
			for w in weeks:
				rows[(pid, w)] = (team, "dst_id")
			continue
		got, bad = resolve(espn.get(pid, {}), nflv.get(pid, {}), weeks, no_game)
		conflicts += [(pid, *c) for c in bad]
		for w, v in got.items():
			rows[(pid, w)] = v
	for pid in sorted({c[0] for c in conflicts})[:20]:
		weeks_bad = [f"wk{w} ESPN {e} nflverse {n}" for p, w, e, n in conflicts if p == pid]
		warnings.append(f"{season} player {pid}: nflverse contradicts played ESPN lines ({'; '.join(weeks_bad[:3])}"
		                f"{' ...' if len(weeks_bad) > 3 else ''}); nflverse ignored for him this season")
	league_pids = set(pids)
	for pid, w, t in [s for s in split if s[0] in league_pids][:20]:
		warnings.append(f"{season} wk{w} player {pid}: two played ESPN lines {t} (left to nflverse/gap fill)")
	for pid in dst_bad:
		warnings.append(f"{season} D/ST {pid}: team {-pid - DST_BASE} not in nfl_teams")

	missing = Counter()
	for pid, w in league_rows_list:
		if w <= last and (pid, w) not in rows:
			missing["box/txn/draft rows"] += 1
	report.update({
		"weeks": f"1-{last}", "players": len(pids), "rows": len(rows),
		"by_source": dict(Counter(s for _, s in rows.values())),
		"league_rows": sum(1 for _, w in league_rows_list if w <= last),
		"league_rows_without_team": missing["box/txn/draft rows"],
		"conflicts": len(conflicts), "nflverse": how, "nflverse_counts": dict(ncounts),
	})
	return teams, rows


async def write_season(conn, season, teams, rows, dry_run, report):
	stored = {(r["espn_player_id"], r["week"]): (r["pro_team_id"], r["source"]) for r in await conn.fetch(
		"SELECT espn_player_id, week, pro_team_id, source FROM player_team_weeks WHERE season = $1", season)}
	new = [k for k in rows if k not in stored]
	changed = [k for k in rows if k in stored and stored[k] != rows[k]]
	gone = [k for k in stored if k not in rows]
	report.update({"stored_before": len(stored), "insert": len(new), "update": len(changed), "delete": len(gone)})
	if dry_run:
		return
	async with conn.transaction():
		await conn.executemany("""
			INSERT INTO nfl_teams (season, pro_team_id, abbrev, location, name, bye_week)
			VALUES ($1, $2, $3, $4, $5, $6)
			ON CONFLICT (season, pro_team_id) DO UPDATE SET abbrev = EXCLUDED.abbrev,
				location = EXCLUDED.location, name = EXCLUDED.name, bye_week = EXCLUDED.bye_week""",
			[(season, i, *v) for i, v in sorted(teams.items())])
		if gone:
			await conn.executemany("DELETE FROM player_team_weeks WHERE espn_player_id = $1 AND season = $2 AND week = $3",
			                       [(pid, season, w) for pid, w in gone])
		if new or changed:
			await conn.executemany("""
				INSERT INTO player_team_weeks (espn_player_id, season, week, pro_team_id, source)
				VALUES ($1, $2, $3, $4, $5)
				ON CONFLICT (espn_player_id, season, week) DO UPDATE SET
					pro_team_id = EXCLUDED.pro_team_id, source = EXCLUDED.source""",
				[(pid, season, w, *rows[(pid, w)]) for pid, w in new + changed])


async def write_players(conn, latest, dry_run, report):
	pool, path = current_pool(latest)
	ever = {r["pid"] for r in await conn.fetch("""
		SELECT espn_player_id pid FROM box_scores UNION SELECT espn_player_id FROM draft_picks
		UNION SELECT espn_player_id FROM transaction_items""")}
	rows = {pid: pool[pid] for pid in ever if pid in pool}
	stored = {r["espn_player_id"]: (r["pro_team_id"], r["team_season"]) for r in await conn.fetch(
		"SELECT espn_player_id, pro_team_id, team_season FROM players")}
	changed = [p for p in rows if stored.get(p) != (rows[p], latest)]
	gone = [p for p in stored if p not in rows]
	report.update({"file": path, "league_players": len(ever), "rows": len(rows),
	               "with_team": sum(1 for t in rows.values() if t), "stored_before": len(stored),
	               "insert_or_update": len(changed), "delete": len(gone)})
	if dry_run:
		return
	async with conn.transaction():
		if gone:
			await conn.execute("DELETE FROM players WHERE espn_player_id = ANY($1::int[])", gone)
		await conn.executemany("""
			INSERT INTO players (espn_player_id, pro_team_id, team_season, updated_at) VALUES ($1, $2, $3, now())
			ON CONFLICT (espn_player_id) DO UPDATE SET pro_team_id = EXCLUDED.pro_team_id,
				team_season = EXCLUDED.team_season, updated_at = now()""",
			[(p, rows[p], latest) for p in changed])


async def run(seasons, dry_run, refresh):
	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	summary, warnings = {"seasons": {}}, []
	try:
		latest = int(await conn.fetchval("SELECT MAX(season) FROM draft_picks"))
		seasons = seasons or list(range(FIRST_SEASON, latest + 1))
		mode = "DRY RUN (no DB writes)" if dry_run else "LIVE"
		print(f"=== NFL team import, seasons {seasons[0]}-{seasons[-1]}, {mode} ===")
		text, how = nflverse_file("players", "players", refresh or latest in seasons, warnings)
		if text is None:
			sys.exit("ERROR: no nflverse players.csv")
		xwalk = crosswalk(text)
		summary["nflverse_players"] = how
		for season in seasons:
			report = {}
			teams, rows = await build_season(conn, season, latest, refresh, xwalk, warnings, report)
			await write_season(conn, season, teams, rows, dry_run, report)
			summary["seasons"][str(season)] = report
			print(f"[{season}] {report['rows']} rows {report['by_source']} | without a team: "
			      f"{report['league_rows_without_team']}/{report['league_rows']} league rows | "
			      f"+{report['insert']} ~{report['update']} -{report['delete']}")
		if latest in seasons:
			summary["players"] = {}
			await write_players(conn, latest, dry_run, summary["players"])
			p = summary["players"]
			print(f"[players] {p['rows']} of {p['league_players']} league players in the {latest} pool, "
			      f"{p['with_team']} on a team | ~{p['insert_or_update']} -{p['delete']}")
	finally:
		await conn.close()

	print("\n=== SUMMARY ===")
	print(json.dumps(summary, indent=2))
	print(f"\n=== WARNINGS ({len(warnings)}) ===" if warnings else "\nNo warnings.")
	for w in warnings:
		print("  - " + w)
	print()
	if dry_run:
		print("\n(DRY RUN: nothing was written to the database.)")


def main():
	ap = argparse.ArgumentParser(description="Import NFL teams (per week, per season, now) into Rockwood.")
	g = ap.add_mutually_exclusive_group(required=True)
	g.add_argument("--season", type=int, action="append", help="season year; repeat for several")
	g.add_argument("--all", action="store_true", help=f"every season from {FIRST_SEASON}")
	ap.add_argument("--dry-run", action="store_true", help="compute and report without writing")
	ap.add_argument("--refresh-nflverse", action="store_true", help="download every nflverse file used")
	args = ap.parse_args()
	asyncio.run(run(sorted(args.season or []), args.dry_run, args.refresh_nflverse))


if __name__ == "__main__":
	main()
