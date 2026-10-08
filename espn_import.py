#!/usr/bin/env python
"""
espn_import.py — import an in-progress ESPN fantasy season into the Rockwood DB.

Usage:
	python espn_import.py --season 2026
	python espn_import.py --season 2026 --dry-run
	python espn_import.py --season 2021 --box-scores-only --weeks 1-13 --dry-run

Imports, in order: espn_team_map -> matchups -> box_scores -> draft_picks.
Only COMPLETED matchups (winner != UNDECIDED) are written. Never writes to
the `records` table. box_scores holds the full roster (starters, bench, IR);
lineup_slot is ESPN's lineupSlotId and is_starter = slot NOT IN (20, 21).
Teams with no matchup that week (playoff byes) get no box_scores rows.
--box-scores-only re-imports box_scores for any season without touching
espn_team_map, matchups or draft_picks. Every ESPN response is archived to
espn_raw/ (see espn_raw.py).
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal

import asyncpg
import requests
from dotenv import load_dotenv

from espn_raw import fetch_archived

load_dotenv()

LEAGUE_ID = 543248
BASE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/543248"
PLAYERS_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/players"

REG_SEASON_WEEKS = 13  # is_playoffs = TRUE only for week >= 14
EXPECTED_STARTERS = 9  # QB, 2x RB, 2x WR, TE, FLEX, K, D/ST

# defaultPositionId -> position label (source of truth for the `position` column)
DEFAULT_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}

# lineupSlotId -> slot label. box_scores.lineup_slot stores the ESPN id itself;
# the labels are only for messages. An id not listed here stops the import.
# (The pre-2026 importer read rosterForMatchupPeriod, which holds starters only
# and reports every slot as 0 — hence the old starters-only rows with 'QB'.)
SLOT_LABEL = {
	0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K", 23: "FLEX",
	20: "BE", 21: "IR",
}
BENCH_SLOTS = {20, 21}  # is_starter = lineup_slot NOT IN (20, 21)


def dec(x):
	"""Coerce a JSON number to Decimal for asyncpg numeric columns."""
	if x is None:
		return None
	return Decimal(str(x))


# --------------------------------------------------------------------------- #
# ESPN HTTP
# --------------------------------------------------------------------------- #
def espn_session():
	s2 = os.getenv("ESPN_S2")
	swid = os.getenv("ESPN_SWID")
	if not s2 or not swid:
		sys.exit("ERROR: ESPN_S2 and ESPN_SWID must be set in .env (raw values, no quotes).")
	sess = requests.Session()
	sess.cookies.set("espn_s2", s2)
	sess.cookies.set("SWID", swid)
	sess.headers.update({"User-Agent": "rockwood-import/1.0"})
	return sess


def fetch_league(sess, year, views, scoring_period=None):
	r = fetch_archived(sess, BASE_URL.format(year=year), year, views, period=scoring_period, timeout=60)
	r.raise_for_status()
	data = r.json()
	if not isinstance(data, dict):
		sys.exit(f"ERROR: expected a dict from league endpoint, got {type(data)}. Check cookies/season.")
	return data


def fetch_players(sess, year):
	hdr = {
		"x-fantasy-filter": json.dumps({
			"players": {
				"limit": 2000,
				"sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
			}
		})
	}
	r = fetch_archived(sess, PLAYERS_URL.format(year=year), year, ["kona_player_info"], headers=hdr, timeout=60)
	r.raise_for_status()
	rows = r.json()
	players = {}
	for p in rows:
		pid = p.get("id")
		if pid is None:
			continue
		players[pid] = {
			"name": p.get("fullName"),
			"pos": DEFAULT_POS.get(p.get("defaultPositionId")),
		}
	return players


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def resolve_name_pos(players, pid, embedded_player):
	"""Prefer the player map; fall back to the player object embedded in the
	boxscore/draft payload. Folds in the old backfill_player_names step."""
	name = None
	pos = None
	if pid in players:
		name = players[pid]["name"]
		pos = players[pid]["pos"]
	if embedded_player:
		name = name or embedded_player.get("fullName")
		pos = pos or DEFAULT_POS.get(embedded_player.get("defaultPositionId"))
	return name, pos


def stat_total(player_obj, week, source):
	"""Pull the week's applied total for a stat source (0 = actual, 1 = projected)."""
	for st in (player_obj or {}).get("stats", []) or []:
		if st.get("scoringPeriodId") == week and st.get("statSourceId") == source:
			return st.get("appliedTotal")
	return None


def actual_points(player_obj, week):
	"""Pull the week's ACTUAL (statSourceId=0) applied total from a player's stats."""
	return stat_total(player_obj, week, 0)


def parse_weeks(spec):
	"""'1-13' / '14,15,16' / '1-3,14' -> set of ints."""
	weeks = set()
	for part in spec.split(","):
		part = part.strip()
		if "-" in part:
			lo, hi = part.split("-", 1)
			weeks.update(range(int(lo), int(hi) + 1))
		elif part:
			weeks.add(int(part))
	return weeks


# --------------------------------------------------------------------------- #
# Step A — espn_team_map
# --------------------------------------------------------------------------- #
async def do_team_map(conn, sess, season, dry_run, summary, warnings):
	data = fetch_league(sess, season, ["mTeam"])
	teams = data.get("teams", [])
	if not teams:
		sys.exit("ERROR: mTeam returned no teams.")

	# historical GUID -> internal team_id (verified: no GUID maps to >1 team_id)
	rows = await conn.fetch("SELECT DISTINCT espn_member_id, team_id FROM espn_team_map")
	resolver = {r["espn_member_id"]: r["team_id"] for r in rows}

	season_map = {}   # espn_team_id -> internal team_id (used by later steps)
	unmatched = []
	upserts = []
	for t in teams:
		espn_team_id = t.get("id")
		abbrev = t.get("abbrev")
		guid = t.get("primaryOwner") or (t.get("owners") or [None])[0]
		internal = resolver.get(guid)
		if internal is None:
			unmatched.append({
				"espn_team_id": espn_team_id,
				"abbrev": abbrev,
				"espn_member_id": guid,
				"location_nickname": f"{t.get('location', '')} {t.get('nickname', '')}".strip(),
			})
			continue
		season_map[espn_team_id] = internal
		upserts.append((internal, dec(season), espn_team_id, guid, abbrev))

	if unmatched:
		print("\n!!! STOPPING: these 2026 teams did not match any known espn_member_id.")
		print("    Resolve manually (add the GUID to espn_team_map for a prior season, or map by hand):")
		for u in unmatched:
			print(f"    espn_team_id={u['espn_team_id']} abbrev={u['abbrev']!r} "
			      f"name={u['location_nickname']!r} member_id={u['espn_member_id']}")
		sys.exit(1)

	if not dry_run:
		for (internal, s, etid, guid, abbrev) in upserts:
			await conn.execute("""
				INSERT INTO espn_team_map (team_id, season, espn_team_id, espn_member_id, team_abbrev)
				VALUES ($1, $2, $3, $4, $5)
				ON CONFLICT (season, espn_team_id)
				DO UPDATE SET team_id = EXCLUDED.team_id,
				              espn_member_id = EXCLUDED.espn_member_id,
				              team_abbrev = EXCLUDED.team_abbrev
			""", internal, s, etid, guid, abbrev)

	summary["espn_team_map"] = len(upserts)
	print(f"[espn_team_map] matched all {len(upserts)} teams -> internal team_ids.")
	return season_map


# --------------------------------------------------------------------------- #
# Step B — matchups
# --------------------------------------------------------------------------- #
async def do_matchups(conn, sess, season, season_map, dry_run, summary, warnings):
	data = fetch_league(sess, season, ["mMatchup", "mMatchupScore"])
	schedule = data.get("schedule", [])

	winners_seen = set()
	decided = []        # list of dicts for decided matchups
	per_week = defaultdict(int)
	for item in schedule:
		winner = item.get("winner", "UNDECIDED")
		winners_seen.add(winner)
		if winner == "UNDECIDED":
			continue
		week = item.get("matchupPeriodId")
		home = item.get("home", {})
		away = item.get("away", {})
		h_espn = home.get("teamId")
		a_espn = away.get("teamId")
		if h_espn not in season_map or a_espn not in season_map:
			warnings.append(f"matchup wk{week}: unknown espn team id(s) {h_espn}/{a_espn}; skipped.")
			continue
		h_id = season_map[h_espn]
		a_id = season_map[a_espn]
		h_score = home.get("totalPoints")
		a_score = away.get("totalPoints")
		if winner == "HOME":
			win_id = h_id
		elif winner == "AWAY":
			win_id = a_id
		else:  # TIE (or anything non-UNDECIDED without a side)
			win_id = None
		decided.append({
			"week": week, "is_playoffs": week >= (REG_SEASON_WEEKS + 1),
			"h_id": h_id, "a_id": a_id,
			"h_score": h_score, "a_score": a_score, "win_id": win_id,
			"winner": winner,
		})
		per_week[week] += 1

	print(f"[matchups] winner values seen in schedule: {sorted(winners_seen)}")

	id_map = {}  # (week, h_id, a_id) -> matchup_id   (only filled on real run)
	ins = upd = 0
	for m in decided:
		if dry_run:
			print(f"  DRY wk{m['week']} playoff={m['is_playoffs']} "
			      f"home {m['h_id']}={m['h_score']} away {m['a_id']}={m['a_score']} "
			      f"winner={m['winner']}({m['win_id']})")
			continue
		row = await conn.fetchrow("""
			INSERT INTO matchups (season, week, is_playoffs, home_team_id, away_team_id,
			                      home_score, away_score, winner_team_id)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
			ON CONFLICT (season, week, home_team_id, away_team_id)
			DO UPDATE SET home_score = EXCLUDED.home_score,
			              away_score = EXCLUDED.away_score,
			              winner_team_id = EXCLUDED.winner_team_id,
			              is_playoffs = EXCLUDED.is_playoffs
			RETURNING id, (xmax = 0) AS inserted
		""", dec(season), m["week"], m["is_playoffs"], m["h_id"], m["a_id"],
		     dec(m["h_score"]), dec(m["a_score"]), m["win_id"])
		id_map[(m["week"], m["h_id"], m["a_id"])] = row["id"]
		if row["inserted"]:
			ins += 1
		else:
			upd += 1

	summary["matchups"] = {"inserted": ins, "updated": upd, "per_week": dict(per_week)}
	completed_weeks = sorted(per_week.keys())
	print(f"[matchups] decided matchups: {len(decided)} across weeks {completed_weeks} "
	      f"(inserted {ins}, updated {upd}).")
	return decided, id_map, completed_weeks


# --------------------------------------------------------------------------- #
# Step C — box_scores
# --------------------------------------------------------------------------- #
async def load_team_map(conn, season):
	"""espn_team_id -> internal team_id from espn_team_map (used by --box-scores-only)."""
	rows = await conn.fetch("SELECT espn_team_id, team_id FROM espn_team_map WHERE season = $1", dec(season))
	if not rows:
		sys.exit(f"ERROR: espn_team_map has no rows for {season}.")
	return {r["espn_team_id"]: r["team_id"] for r in rows}


async def load_matchups(conn, season):
	"""Matchups already in the DB for a season, in the shape do_box_scores expects."""
	rows = await conn.fetch("""
		SELECT id, week, home_team_id, away_team_id, home_score, away_score
		FROM matchups WHERE season = $1
	""", dec(season))
	return [{"id": r["id"], "week": r["week"], "h_id": r["home_team_id"], "a_id": r["away_team_id"],
	         "h_score": r["home_score"], "a_score": r["away_score"]} for r in rows]


async def do_box_scores(conn, sess, season, season_map, matchups, players, weeks,
                        dry_run, summary, warnings):
	"""Full roster (starters, bench, IR) for every matchup in `matchups`.

	matchups: [{"id", "week", "h_id", "a_id", "h_score", "a_score"}]; id is None
	on a full-mode dry run for matchups not yet in the DB. Teams with no matchup
	that week (playoff byes) are skipped; their rosters stay in the raw archive.
	Existing rows keep their player_name/position; everything else is updated.
	"""
	by_week = defaultdict(list)
	for m in matchups:
		if weeks is None or m["week"] in weeks:
			by_week[m["week"]].append(m)
	internal_to_espn = {v: k for k, v in season_map.items()}

	# --- gather everything first; nothing is written until every week parses ---
	rows = []
	per_week = {}
	bye_sides = defaultdict(int)
	for week in sorted(by_week.keys()):
		data = fetch_league(sess, season, ["mBoxscore", "mMatchupScore"], scoring_period=week)
		box_index = {}
		for item in data.get("schedule", []):
			if item.get("matchupPeriodId") != week:
				continue
			if not item.get("home") or not item.get("away"):
				bye_sides[week] += 1
				continue
			box_index[(item["home"].get("teamId"), item["away"].get("teamId"))] = item

		counts = {"starters": 0, "bench": 0, "ir": 0, "no_stats": 0}
		for m in by_week[week]:
			item = box_index.get((internal_to_espn.get(m["h_id"]), internal_to_espn.get(m["a_id"])))
			if item is None:
				warnings.append(f"box wk{week}: no boxscore item for internal {m['h_id']} vs {m['a_id']}; skipped.")
				continue

			for side, internal_id, score in (("home", m["h_id"], m["h_score"]), ("away", m["a_id"], m["a_score"])):
				side_obj = item.get(side, {}) or {}
				roster = (side_obj.get("rosterForCurrentScoringPeriod", {}) or {}).get("entries", [])
				n_starters = 0
				starter_sum = Decimal("0")
				for e in roster:
					slot = e.get("lineupSlotId")
					if slot not in SLOT_LABEL:
						sys.exit(f"ERROR: unknown lineupSlotId {slot!r} (season {season} wk{week} team {internal_id}). Nothing written.")
					pid = e.get("playerId")
					ppe = e.get("playerPoolEntry", {}) or {}
					pobj = ppe.get("player", {}) or {}
					name, pos = resolve_name_pos(players, pid, pobj)
					pts = actual_points(pobj, week)
					has_stats = pts is not None
					if pts is None:
						pts = ppe.get("appliedStatTotal")
					if pts is None:
						warnings.append(f"box wk{week} team {internal_id}: no points at all for playerId {pid}; stored 0.")
						pts = 0
					if name is None:
						warnings.append(f"box wk{week} team {internal_id}: no name for playerId {pid}; skipped row.")
						continue
					is_starter = slot not in BENCH_SLOTS
					if is_starter:
						n_starters += 1
						starter_sum += dec(pts)
						counts["starters"] += 1
					elif slot == 21:
						counts["ir"] += 1
					else:
						counts["bench"] += 1
					if not has_stats:
						counts["no_stats"] += 1
					rows.append({
						"matchup_id": m["id"], "team_id": internal_id, "week": week,
						"espn_player_id": pid, "player_name": name, "position": pos,
						"lineup_slot": slot, "is_starter": is_starter,
						"points": dec(pts), "projected": dec(stat_total(pobj, week, 1)),
						"has_stats": has_stats,
					})

				if n_starters != EXPECTED_STARTERS:
					warnings.append(f"box wk{week} team {internal_id}: {n_starters} starters "
					                f"(expected {EXPECTED_STARTERS}) — empty lineup slot(s).")
				espn_total = (side_obj.get("pointsByScoringPeriod") or {}).get(str(week))
				if espn_total is not None and abs(starter_sum - dec(espn_total)) > Decimal("0.005"):
					warnings.append(f"box wk{week} team {internal_id}: starter sum {starter_sum} != ESPN {espn_total}.")
				if score is not None and abs(starter_sum - dec(score)) > Decimal("0.005"):
					warnings.append(f"box wk{week} team {internal_id}: starter sum {starter_sum} != matchup score {score}.")

		per_week[week] = counts
		print(f"[box_scores] wk{week}: {counts['starters']} starters, {counts['bench']} bench, "
		      f"{counts['ir']} IR ({counts['no_stats']} without a stat line); "
		      f"{bye_sides[week]} bye side(s) skipped" + (" (dry)" if dry_run else ""))

	# --- compare against what is already stored ---
	existing = {}
	for r in await conn.fetch("""
		SELECT matchup_id, team_id, espn_player_id, week, player_name, position, points_scored, is_starter
		FROM box_scores WHERE season = $1
	""", dec(season)):
		if weeks is None or r["week"] in weeks:
			existing[(r["matchup_id"], r["team_id"], r["espn_player_id"], r["week"])] = r
	existing_sides = {(k[0], k[1]) for k in existing}  # team-games that already had rows
	diffs = {"points": [], "name": [], "position": [], "starter_now_bench": [], "new_starter_rows": []}
	seen = set()
	for r in rows:
		key = (r["matchup_id"], r["team_id"], r["espn_player_id"], r["week"])
		old = existing.get(key)
		if old is None:
			if r["is_starter"] and (r["matchup_id"], r["team_id"]) in existing_sides:
				diffs["new_starter_rows"].append(
					f"wk{r['week']} team {r['team_id']} pid {r['espn_player_id']} {r['player_name']!r} "
					f"{r['position']} slot {SLOT_LABEL[r['lineup_slot']]} pts {r['points']} has_stats={r['has_stats']}")
			continue
		seen.add(key)
		tag = f"wk{r['week']} team {r['team_id']} pid {r['espn_player_id']} {old['player_name']!r}"
		if old["points_scored"] != r["points"]:
			diffs["points"].append(f"{tag}: {old['points_scored']} -> {r['points']}")
		if old["player_name"] != r["player_name"]:
			diffs["name"].append(f"{tag}: kept {old['player_name']!r} (ESPN now {r['player_name']!r})")
		if old["position"] != r["position"]:
			diffs["position"].append(f"{tag}: kept {old['position']!r} (ESPN now {r['position']!r})")
		if old["is_starter"] and not r["is_starter"]:
			diffs["starter_now_bench"].append(f"{tag}: stored as starter, ESPN slot {SLOT_LABEL[r['lineup_slot']]}")
	not_returned = [f"wk{k[3]} team {k[1]} pid {k[2]} {v['player_name']!r}"
	                for k, v in sorted(existing.items(), key=lambda kv: (kv[0][3], kv[0][1], kv[0][2]))
	                if k not in seen]

	# --- write ---
	ins = upd = 0
	if not dry_run:
		async with conn.transaction():
			for r in rows:
				res = await conn.fetchrow("""
					INSERT INTO box_scores (matchup_id, team_id, season, week, espn_player_id,
					                        player_name, nfl_team, position, lineup_slot,
					                        is_starter, points_scored, projected_points, has_stats)
					VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, $8, $9, $10, $11, $12)
					ON CONFLICT (matchup_id, team_id, espn_player_id, week)
					DO UPDATE SET points_scored = EXCLUDED.points_scored,
					              projected_points = EXCLUDED.projected_points,
					              is_starter = EXCLUDED.is_starter,
					              lineup_slot = EXCLUDED.lineup_slot,
					              has_stats = EXCLUDED.has_stats
					RETURNING (xmax = 0) AS inserted
				""", r["matchup_id"], r["team_id"], dec(season), r["week"], r["espn_player_id"],
				     r["player_name"], r["position"], r["lineup_slot"], r["is_starter"],
				     r["points"], r["projected"], r["has_stats"])
				if res["inserted"]:
					ins += 1
				else:
					upd += 1

	summary["box_scores"] = {
		"rows_seen": len(rows), "inserted": ins, "updated": upd, "per_week": per_week,
		"bye_sides_skipped": dict(bye_sides),
		"existing_rows_matched": len(seen), "existing_rows_not_returned": len(not_returned),
		"diff_counts": {k: len(v) for k, v in diffs.items()},
	}
	for label, items in list(diffs.items()) + [("existing rows ESPN did not return", not_returned)]:
		if items:
			print(f"\n[box_scores] {label} ({len(items)}):")
			for s in items:
				print("    " + s)


# --------------------------------------------------------------------------- #
# Step D — draft_picks
# --------------------------------------------------------------------------- #
async def do_draft(conn, sess, season, season_map, players, dry_run, summary, warnings):
	data = fetch_league(sess, season, ["mDraftDetail"])
	detail = data.get("draftDetail", {}) or {}
	picks = detail.get("picks", []) or []
	if not picks:
		warnings.append("draft: mDraftDetail returned no picks (draft may not have happened yet).")
		summary["draft_picks"] = {"inserted": 0, "count_seen": 0}
		print("[draft_picks] no picks returned.")
		return

	ins = 0
	for p in picks:
		overall = p.get("overallPickNumber")
		rnd = p.get("roundId")
		pir = p.get("roundPickNumber")
		espn_team_id = p.get("teamId")
		pid = p.get("playerId")
		keeper = bool(p.get("keeper", False))
		internal = season_map.get(espn_team_id)
		if internal is None:
			warnings.append(f"draft pick {overall}: unknown espn team id {espn_team_id}; skipped.")
			continue
		name, pos = resolve_name_pos(players, pid, None)
		if dry_run:
			print(f"  DRY pick {overall} r{rnd}.{pir} team {internal} "
			      f"{name!r} {pos} keeper={keeper}")
			continue
		result = await conn.execute("""
			INSERT INTO draft_picks (season, team_id, overall_pick, round_num, pick_in_round,
			                         espn_player_id, player_name, position, is_keeper)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
			ON CONFLICT (season, overall_pick) DO NOTHING
		""", dec(season), internal, overall, rnd, pir, pid, name, pos, keeper)
		if result.endswith("1"):
			ins += 1

	summary["draft_picks"] = {"inserted": ins, "count_seen": len(picks)}
	print(f"[draft_picks] {len(picks)} picks seen, inserted {ins} (existing left untouched).")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def run(season, dry_run, weeks=None, box_scores_only=False):
	sess = espn_session()
	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	summary = {}
	warnings = []
	try:
		mode = "DRY RUN (no DB writes)" if dry_run else "LIVE"
		scope = " — box_scores only" if box_scores_only else ""
		wk = f" — weeks {sorted(weeks)}" if weeks else ""
		print(f"=== ESPN import season {season} — {mode}{scope}{wk} ===")

		if box_scores_only:
			# Historical re-import: team map and matchups come from the DB and are not touched.
			season_map = await load_team_map(conn, season)
			players = fetch_players(sess, season)
			print(f"[players] loaded {len(players)} player names.")
			matchups = await load_matchups(conn, season)
			await do_box_scores(conn, sess, season, season_map, matchups, players, weeks,
			                    dry_run, summary, warnings)
		else:
			season_map = await do_team_map(conn, sess, season, dry_run, summary, warnings)
			players = fetch_players(sess, season)
			print(f"[players] loaded {len(players)} player names.")

			decided, id_map, completed_weeks = await do_matchups(
				conn, sess, season, season_map, dry_run, summary, warnings)
			# id_map is empty on a dry run; fall back to matchups already in the DB so
			# existing box_scores rows can still be compared (None only for new matchups).
			db_ids = {(m["week"], m["h_id"], m["a_id"]): m["id"] for m in await load_matchups(conn, season)}
			matchups = [{"id": id_map.get((m["week"], m["h_id"], m["a_id"])) or db_ids.get((m["week"], m["h_id"], m["a_id"])),
			             "week": m["week"], "h_id": m["h_id"], "a_id": m["a_id"],
			             "h_score": m["h_score"], "a_score": m["a_score"]} for m in decided]
			await do_box_scores(conn, sess, season, season_map, matchups, players, weeks,
			                    dry_run, summary, warnings)
			await do_draft(conn, sess, season, season_map, players, dry_run, summary, warnings)
	finally:
		await conn.close()

	print("\n=== SUMMARY ===")
	print(json.dumps(summary, indent=2, default=str))
	if warnings:
		print(f"\n=== WARNINGS ({len(warnings)}) ===")
		for w in warnings:
			print("  - " + w)
	else:
		print("\nNo warnings.")
	if dry_run:
		print("\n(DRY RUN — nothing was written to the database.)")


def main():
	ap = argparse.ArgumentParser(description="Import an in-progress ESPN season into Rockwood.")
	ap.add_argument("--season", type=int, required=True, help="season year, e.g. 2026")
	ap.add_argument("--dry-run", action="store_true", help="fetch and report without writing to the DB")
	ap.add_argument("--weeks", help="limit box_scores to these weeks, e.g. 1-13 or 14,15,16")
	ap.add_argument("--box-scores-only", action="store_true",
	                help="re-import box_scores only, using espn_team_map and matchups already in the DB")
	args = ap.parse_args()
	weeks = parse_weeks(args.weeks) if args.weeks else None
	asyncio.run(run(args.season, args.dry_run, weeks, args.box_scores_only))


if __name__ == "__main__":
	main()
