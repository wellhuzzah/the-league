#!/usr/bin/env python
"""
espn_import.py — import an in-progress ESPN fantasy season into the Rockwood DB.

Usage:
	python espn_import.py --season 2026
	python espn_import.py --season 2026 --dry-run

Imports, in order: espn_team_map -> matchups -> box_scores -> draft_picks.
Only COMPLETED matchups (winner != UNDECIDED) are written. Never writes to
the `records` table. See the conventions enforced below (starters only, etc.).
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

load_dotenv()

LEAGUE_ID = 543248
BASE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/543248"
PLAYERS_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/players"

REG_SEASON_WEEKS = 13  # is_playoffs = TRUE only for week >= 14
EXPECTED_STARTERS = 9  # QB, 2x RB, 2x WR, TE, FLEX, K, D/ST

# defaultPositionId -> position label (source of truth for the `position` column)
DEFAULT_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}

# lineupSlotId -> slot label (source for the `lineup_slot` column; nothing in the
# app reads this column, so we store the real slot rather than the legacy 'QB').
SLOT_LABEL = {
	0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K", 23: "FLEX",
	20: "BE", 21: "IR",
}
BENCH_SLOTS = {20, 21}  # excluded; everything else counts as a starter


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
	params = [("view", v) for v in views]
	if scoring_period is not None:
		params.append(("scoringPeriodId", scoring_period))
	r = sess.get(BASE_URL.format(year=year), params=params, timeout=60)
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
	r = sess.get(PLAYERS_URL.format(year=year), params={"view": "kona_player_info"}, headers=hdr, timeout=60)
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


def actual_points(player_obj, week):
	"""Pull the week's ACTUAL (statSourceId=0) applied total from a player's stats."""
	for st in (player_obj or {}).get("stats", []) or []:
		if st.get("scoringPeriodId") == week and st.get("statSourceId") == 0:
			return st.get("appliedTotal")
	return None


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
async def do_box_scores(conn, sess, season, season_map, decided, id_map,
                        players, dry_run, summary, warnings):
	# group decided matchups by week
	by_week = defaultdict(list)
	for m in decided:
		by_week[m["week"]].append(m)

	per_week_rows = {}
	ins = upd = 0
	for week in sorted(by_week.keys()):
		data = fetch_league(sess, season, ["mBoxscore", "mMatchupScore"], scoring_period=week)
		schedule = data.get("schedule", [])
		# index boxscore schedule items by (home_espn, away_espn) for this week
		box_index = {}
		for item in schedule:
			if item.get("matchupPeriodId") != week:
				continue
			box_index[(item.get("home", {}).get("teamId"),
			           item.get("away", {}).get("teamId"))] = item

		week_rows = 0
		# invert season_map to find espn ids for a given internal matchup
		internal_to_espn = {v: k for k, v in season_map.items()}
		for m in by_week[week]:
			h_espn = internal_to_espn.get(m["h_id"])
			a_espn = internal_to_espn.get(m["a_id"])
			item = box_index.get((h_espn, a_espn))
			if item is None:
				warnings.append(f"box wk{week}: no boxscore item for internal {m['h_id']} vs {m['a_id']}; skipped.")
				continue
			matchup_id = id_map.get((week, m["h_id"], m["a_id"]))  # None on dry run

			for side, internal_id in (("home", m["h_id"]), ("away", m["a_id"])):
				roster = (item.get(side, {}).get("rosterForCurrentScoringPeriod", {}) or {}).get("entries", [])
				starters = []
				for e in roster:
					slot = e.get("lineupSlotId")
					if slot in BENCH_SLOTS:
						continue
					pid = e.get("playerId")
					ppe = e.get("playerPoolEntry", {}) or {}
					pobj = ppe.get("player", {}) or {}
					name, pos = resolve_name_pos(players, pid, pobj)
					pts = actual_points(pobj, week)
					if pts is None:
						pts = ppe.get("appliedStatTotal")
					if name is None:
						warnings.append(f"box wk{week} team {internal_id}: no name for playerId {pid}; skipped row.")
						continue
					starters.append({
						"espn_player_id": pid,
						"player_name": name,
						"position": pos,
						"lineup_slot": SLOT_LABEL.get(slot, str(slot)),
						"points": pts,
					})

				if len(starters) != EXPECTED_STARTERS:
					warnings.append(f"box wk{week} team {internal_id}: {len(starters)} starters "
					                f"(expected {EXPECTED_STARTERS}) — check lineupSlotId data.")

				for s in starters:
					week_rows += 1
					if dry_run:
						continue
					row = await conn.fetchrow("""
						INSERT INTO box_scores (matchup_id, team_id, season, week, espn_player_id,
						                        player_name, nfl_team, position, lineup_slot,
						                        is_starter, points_scored, projected_points)
						VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, $8, TRUE, $9, NULL)
						ON CONFLICT (matchup_id, team_id, espn_player_id, week)
						DO UPDATE SET points_scored = EXCLUDED.points_scored,
						              projected_points = EXCLUDED.projected_points,
						              is_starter = EXCLUDED.is_starter,
						              lineup_slot = EXCLUDED.lineup_slot,
						              position = EXCLUDED.position,
						              player_name = EXCLUDED.player_name
						RETURNING (xmax = 0) AS inserted
					""", matchup_id, internal_id, dec(season), week, s["espn_player_id"],
					     s["player_name"], s["position"], s["lineup_slot"], dec(s["points"]))
					if row["inserted"]:
						ins += 1
					else:
						upd += 1

		per_week_rows[week] = week_rows
		print(f"[box_scores] wk{week}: {week_rows} starter rows"
		      + ("" if not dry_run else " (dry)"))

	summary["box_scores"] = {"inserted": ins, "updated": upd, "per_week": per_week_rows}


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
async def run(season, dry_run):
	sess = espn_session()
	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	summary = {}
	warnings = []
	try:
		mode = "DRY RUN (no DB writes)" if dry_run else "LIVE"
		print(f"=== ESPN import season {season} — {mode} ===")

		season_map = await do_team_map(conn, sess, season, dry_run, summary, warnings)
		players = fetch_players(sess, season)
		print(f"[players] loaded {len(players)} player names.")

		decided, id_map, completed_weeks = await do_matchups(
			conn, sess, season, season_map, dry_run, summary, warnings)
		await do_box_scores(conn, sess, season, season_map, decided, id_map,
		                    players, dry_run, summary, warnings)
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
	args = ap.parse_args()
	asyncio.run(run(args.season, args.dry_run))


if __name__ == "__main__":
	main()
