#!/usr/bin/env python
"""
espn_transactions_import.py — import ESPN waiver / free agent / drop history into
`transactions` (one row per ESPN transaction) and `transaction_items` (one row per player).

Usage:
	python espn_transactions_import.py --season 2025 --dry-run
	python espn_transactions_import.py --all --dry-run
	python espn_transactions_import.py --season 2026          # safe to re-run weekly

Source: view=mTransactions2&scoringPeriodId=W, one call per scoring period, swept
from 0 through status.latestScoringPeriod. Seasons before 2018 are not served.
Every response is archived through espn_raw.fetch_archived (via espn_import.fetch_league).

Kept: WAIVER and FREEAGENT rows, and ROSTER rows that contain a DROP item.
Classification, in order:
	1. executionType CANCEL -> stored with status CANCELED
	2. isPending            -> skipped (owner's own submissions / unprocessed bids)
	3. status PENDING       -> skipped and reported
	4. otherwise            -> stored with status as given
Team comes from the items (ADD toTeamId, else DROP fromTeamId), never the
top-level teamId. Free agent pool is team 0 (2025) or -1 (2018).
"""

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import asyncpg

from espn_import import dec, espn_session, fetch_league, fetch_players

SEASONS = list(range(2018, 2027))
KEEP_TYPES = {"WAIVER", "FREEAGENT", "ROSTER"}
NO_TEAM = {0, -1}
# A system CANCEL twin is proposed within a millisecond of its failed claim; allow a second.
TWIN_MAX_GAP_S = 1.0

# Known answers from Postman (handoff). Period = the scoringPeriodId that was queried.
KNOWN = {
	2025: {
		16: {"waiver_rows": 8, "executed_bids": [23, 10, 5, 1], "invalid_source_bids": [5, 2, 1],
		     "canceled": 1, "freeagent_rows": 6, "roster_drops": 4, "skipped_FUTURE_ROSTER": 3},
		5: {"executed_on": {"2025-10-01": 12}, "failed": 9, "canceled": 2, "skipped_isPending": 3},
		1: {"executed_on": {"2025-08-11": 3, "2025-08-13": 1}, "status_on": {("2025-08-11", "FAILED_ROSTERLIMIT"): 1}},
	},
	2018: {
		1: {"executed_on": {"2018-08-26": 3}, "executed_team_bids": [(9, 1), (11, 4), (13, 1)]},
		5: {"waiver_rows": 23, "executed": 13, "executed_on": {"2018-10-03": 12, "2018-10-04": 1},
		    "canceled_bids": [1, 1, 1, 1, 2, 5, 6, 6, 8], "status_counts": {"FAILED_MATCHUPACQUISITIONLIMIT": 1,
		    "FAILED_INVALIDPLAYERSOURCE": 0}},
	},
}


class SeasonStop(Exception):
	pass


def ms_to_dt(ms):
	if ms is None:
		return None
	return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def utc_date(dt):
	return dt.strftime("%Y-%m-%d") if dt else None


# --------------------------------------------------------------------------- #
# fetch + classify
# --------------------------------------------------------------------------- #
def classify(t, team_map, players, stats, log):
	"""Return a row dict to store, or None (counted in stats)."""
	ttype = t.get("type")
	items = t.get("items") or []
	adds = [i for i in items if i.get("type") == "ADD"]
	drops = [i for i in items if i.get("type") == "DROP"]

	if ttype not in KEEP_TYPES:
		stats["skipped_type"][ttype] += 1
		if ttype == "FUTURE_ROSTER":
			stats["future_roster_by_period"][t["_query_period"]] += 1
		return None
	if ttype == "ROSTER" and not drops:
		stats["skipped_type"]["ROSTER (no DROP)"] += 1
		return None

	# classification rules, in order
	if t.get("executionType") == "CANCEL":
		status = "CANCELED"
		rule = "1 CANCEL"
		if t.get("isPending"):
			stats["cancel_and_isPending"] += 1
	elif t.get("isPending"):
		stats["skipped_rule"]["2 isPending"] += 1
		stats["skipped_isPending_by_period"][t["_query_period"]] += 1
		return None
	elif t.get("status") == "PENDING":
		stats["skipped_rule"]["3 PENDING"] += 1
		log.append(f"rule 3 PENDING skipped: {ttype} {t.get('id')} period {t.get('scoringPeriodId')}")
		return None
	else:
		status = t.get("status")
		rule = "4 as given"
	stats["kept_rule"][rule] += 1

	# payload rules: the team comes from the items (every ADD's toTeamId and every
	# DROP's fromTeamId must name the same team), never from the top-level teamId.
	if not adds and not drops:
		stats["skipped_rule"]["no ADD or DROP"] += 1
		log.append(f"no ADD/DROP skipped: {ttype} {t.get('id')}")
		return None
	item_teams = {i.get("toTeamId") for i in adds} | {i.get("fromTeamId") for i in drops}
	if len(item_teams) > 1:
		stats["skipped_rule"]["ADD/DROP team conflict"] += 1
		log.append(f"ADD/DROP team conflict skipped: {t.get('id')} item teams {sorted(item_teams, key=str)}")
		return None
	espn_team = next(iter(item_teams))
	if espn_team in NO_TEAM or espn_team is None:
		stats["skipped_rule"]["no team in items"] += 1
		log.append(f"no team in items skipped: {ttype} {t.get('id')} team={espn_team}")
		return None
	team_id = team_map.get(espn_team)
	if team_id is None:
		raise SeasonStop(f"ESPN team {espn_team} not in espn_team_map (txn {t.get('id')})")
	if t.get("teamId") != espn_team:
		stats["toplevel_team_differs"] += 1
	if len(adds) > 1 or len(drops) > 1:
		stats["multi_player"].append(f"{ttype} {t.get('status')} wk{t.get('scoringPeriodId')} ESPN team {espn_team}: "
		                             f"{len(adds)} ADD, {len(drops)} DROP ({t.get('id')})")

	proposed = ms_to_dt(t.get("proposedDate"))
	if proposed is None:
		stats["skipped_rule"]["no proposedDate"] += 1
		log.append(f"no proposedDate skipped: {t.get('id')}")
		return None
	processed = ms_to_dt(t.get("processDate"))
	if processed is not None and processed < proposed:
		stats["processed_before_proposed"] += 1

	def internal(espn_id):
		"""ESPN team id on an item -> internal team_id; the free agent pool -> None."""
		if espn_id in NO_TEAM or espn_id is None:
			return None
		tid = team_map.get(espn_id)
		if tid is None:
			raise SeasonStop(f"ESPN team {espn_id} not in espn_team_map (item in txn {t.get('id')})")
		return tid

	items_out = []
	for i in adds + drops:
		pid = i.get("playerId")
		p = players.get(pid)
		if p is None:
			stats["unresolved_players"].add(pid)
		items_out.append({
			"item_type": i.get("type"), "espn_player_id": pid,
			"player_name": p["name"] if p else None, "position": p["pos"] if p else None,
			"from_team_id": internal(i.get("fromTeamId")), "to_team_id": internal(i.get("toTeamId")),
		})
	return {
		"espn_txn_id": t["id"], "espn_related_txn_id": t.get("relatedTransactionId"), "cancel_twin_of": None,
		"week": t.get("scoringPeriodId"), "team_id": team_id, "espn_team_id": espn_team,
		"type": ttype, "status": status, "bid_amount": t.get("bidAmount"),
		"proposed_at": proposed, "processed_at": processed, "items": items_out,
		"_query_period": t["_query_period"], "_raw_status": t.get("status"),
		"_exec": t.get("executionType"),
	}


def item_key(r):
	return tuple(sorted((i["item_type"], i["espn_player_id"]) for i in r["items"]))


def link_cancel_twins(rows, stats):
	"""A failed claim can also appear as a system CANCEL row with no relatedTransactionId (2018).
	Pair each such CANCEL row with a FAILED_* WAIVER row of the same week, team, ADD/DROP items
	and bid, proposed within TWIN_MAX_GAP_S of it (the closest one), one-to-one, and record the
	pairing on the CANCEL row (cancel_twin_of). A CANCEL with the same items but a different bid
	or time is a real user cancellation of an earlier claim, not a twin."""
	failed = defaultdict(list)
	for r in rows:
		if r["type"] == "WAIVER" and r["status"].startswith("FAILED"):
			failed[(r["week"], r["team_id"], item_key(r))].append(r)
	for r in rows:
		if r["type"] == "WAIVER" and r["_exec"] == "CANCEL" and not r["espn_related_txn_id"]:
			cands = failed.get((r["week"], r["team_id"], item_key(r)), [])
			gap = lambda f: abs((f["proposed_at"] - r["proposed_at"]).total_seconds())
			f = min((f for f in cands if f["bid_amount"] == r["bid_amount"] and gap(f) <= TWIN_MAX_GAP_S),
			        key=gap, default=None)
			if f is not None:
				cands.remove(f)
				r["cancel_twin_of"] = f["espn_txn_id"]
				stats["twins_linked"] += 1
	stats["failed_without_twin"] = sum(len(v) for v in failed.values())


def new_stats():
	return {
		"skipped_type": Counter(), "skipped_rule": Counter(), "kept_rule": Counter(),
		"skipped_isPending_by_period": Counter(), "future_roster_by_period": Counter(), "cancel_and_isPending": 0,
		"toplevel_team_differs": 0, "processed_before_proposed": 0, "unresolved_players": set(),
		"duplicates_across_periods": 0, "period_mismatch": 0, "draft_rows": 0, "raw_statuses": Counter(),
		"multi_player": [], "twins_linked": 0, "failed_without_twin": 0,
	}


async def import_season(conn, sess, season, dry_run):
	print(f"\n================ {season} ================")
	rows_db = await conn.fetch("SELECT espn_team_id, team_id FROM espn_team_map WHERE season = $1", dec(season))
	team_map = {r["espn_team_id"]: r["team_id"] for r in rows_db}
	if not team_map:
		raise SeasonStop("no espn_team_map rows")
	players = fetch_players(sess, season)
	teams = fetch_league(sess, season, ["mTeam"]).get("teams", [])
	budget = {t["id"]: (t.get("transactionCounter") or {}).get("acquisitionBudgetSpent") for t in teams}

	first = fetch_league(sess, season, ["mTransactions2"], scoring_period=0)
	status = first.get("status", {})
	latest = status.get("latestScoringPeriod")
	wps = status.get("waiverProcessStatus") or {}
	print(f"[sweep] scoringPeriodId 0..{latest}; players loaded {len(players)}")

	stats = new_stats()
	log = []
	seen = {}
	rows = []
	for w in range(0, latest + 1):
		data = first if w == 0 else fetch_league(sess, season, ["mTransactions2"], scoring_period=w)
		for t in data.get("transactions", []) or []:
			if t.get("id") in seen:
				stats["duplicates_across_periods"] += 1
				continue
			seen[t.get("id")] = w
			t["_query_period"] = w
			stats["raw_statuses"][(t.get("type"), t.get("status"), t.get("executionType"))] += 1
			if t.get("type") == "DRAFT":
				stats["draft_rows"] += 1
			if t.get("scoringPeriodId") != w:
				stats["period_mismatch"] += 1
			row = classify(t, team_map, players, stats, log)
			if row is not None:
				rows.append(row)

	link_cancel_twins(rows, stats)
	report(season, rows, stats, log, wps, budget, team_map)
	draft_db = await conn.fetchval("SELECT COUNT(*) FROM draft_picks WHERE season = $1", dec(season))
	print(f"[truncation] DRAFT rows returned {stats['draft_rows']} vs draft_picks in DB {draft_db}"
	      f" -> {'OK' if stats['draft_rows'] == draft_db else 'MISMATCH'}")
	check_known(season, rows, stats)

	if not dry_run:
		await write_rows(conn, season, rows)
	return rows, stats


# --------------------------------------------------------------------------- #
# validation output
# --------------------------------------------------------------------------- #
def report(season, rows, stats, log, wps, budget, team_map):
	by_ts = Counter((r["type"], r["status"]) for r in rows)
	print(f"[kept] {len(rows)} rows by type/status:")
	for (ty, st), n in sorted(by_ts.items()):
		print(f"    {ty:<9} {st:<34} {n}")
	ic = Counter(i["item_type"] for r in rows for i in r["items"])
	print(f"[kept] {sum(ic.values())} transaction_items: {ic['ADD']} ADD, {ic['DROP']} DROP")
	print(f"[kept] by classification rule: {dict(stats['kept_rule'])}")
	print(f"[skipped] by type: {dict(stats['skipped_type'])}")
	print(f"[skipped] by rule: {dict(stats['skipped_rule'])}")
	print(f"[check] FAILED_INVALIDPLAYERSOURCE rows (losing bids): {by_ts[('WAIVER', 'FAILED_INVALIDPLAYERSOURCE')]}")
	print(f"[check] CANCEL rows that were also isPending (kept by rule 1): {stats['cancel_and_isPending']}")
	print(f"[check] top-level teamId differs from item team: {stats['toplevel_team_differs']}")
	print(f"[check] processDate earlier than proposedDate: {stats['processed_before_proposed']}")
	print(f"[check] duplicate ids across periods: {stats['duplicates_across_periods']}; "
	      f"txn scoringPeriodId != queried period: {stats['period_mismatch']}")
	print(f"[check] unresolved player ids (stored with NULL name): {len(stats['unresolved_players'])}")
	n_failed = sum(1 for r in rows if r["type"] == "WAIVER" and r["status"].startswith("FAILED"))
	print(f"[check] failed claims linked to a system CANCEL twin (cancel_twin_of): {stats['twins_linked']} "
	      f"of {n_failed} FAILED_* WAIVER rows")
	print(f"[check] multi-player moves stored: {len(stats['multi_player'])}")
	for m in stats["multi_player"]:
		print("    " + m)

	# executed waiver claims per UTC date vs waiverProcessStatus
	ours = Counter(utc_date(r["processed_at"]) for r in rows if r["type"] == "WAIVER" and r["status"] == "EXECUTED")
	espn = Counter()
	negative = []
	for k, v in wps.items():
		espn[k[:10]] += v
		if v < 0:
			negative.append(f"{k}={v}")
	dates = sorted(set(ours) | set(espn), key=lambda d: d or "")
	mism = [f"{d}: ours {ours.get(d, 0)} vs ESPN {espn.get(d, 0)}" for d in dates if ours.get(d, 0) != espn.get(d, 0)]
	print(f"[waivers] executed claims by UTC date: {len(dates)} dates, {sum(ours.values())} ours vs "
	      f"{sum(v for v in espn.values())} ESPN; mismatched dates: {len(mism)}"
	      + (f"; negative ESPN values: {negative}" if negative else ""))
	for m in mism:
		print("    " + m)

	# FAAB spent per team
	spent_w = defaultdict(int)
	spent_wf = defaultdict(int)
	for r in rows:
		if r["status"] != "EXECUTED" or not r["bid_amount"]:
			continue
		if r["type"] == "WAIVER":
			spent_w[r["espn_team_id"]] += r["bid_amount"]
		if r["type"] in ("WAIVER", "FREEAGENT"):
			spent_wf[r["espn_team_id"]] += r["bid_amount"]
	ok_w = ok_wf = 0
	lines = []
	for et in sorted(budget):
		b = budget[et]
		mw, mwf = spent_w[et] == b, spent_wf[et] == b
		ok_w += mw
		ok_wf += mwf
		if not (mw and mwf):
			lines.append(f"    ESPN team {et} (team_id {team_map.get(et)}): spent {b}; WAIVER {spent_w[et]}"
			             f"{'' if mw else ' X'}; WAIVER+FA {spent_wf[et]}{'' if mwf else ' X'}")
	print(f"[faab] teams matching acquisitionBudgetSpent: WAIVER only {ok_w}/{len(budget)}, "
	      f"WAIVER+FREEAGENT {ok_wf}/{len(budget)}")
	for l in lines:
		print(l)
	if log:
		print(f"[log] {len(log)} skipped-row notes:")
		for l in log[:25]:
			print("    " + l)
		if len(log) > 25:
			print(f"    ... {len(log) - 25} more")


def check_known(season, rows, stats):
	if season not in KNOWN:
		return
	print(f"[known answers] {season}:")
	for period, exp in sorted(KNOWN[season].items(), reverse=True):
		pr = [r for r in rows if r["_query_period"] == period]
		wv = [r for r in pr if r["type"] == "WAIVER"]
		ex = [r for r in wv if r["status"] == "EXECUTED"]
		got = {}
		for key in exp:
			if key == "waiver_rows":
				got[key] = len(wv)
			elif key == "executed":
				got[key] = len(ex)
			elif key == "executed_bids":
				got[key] = sorted((r["bid_amount"] for r in ex), reverse=True)
			elif key == "invalid_source_bids":
				got[key] = sorted((r["bid_amount"] for r in wv if r["status"] == "FAILED_INVALIDPLAYERSOURCE"), reverse=True)
			elif key == "canceled":
				got[key] = sum(1 for r in wv if r["status"] == "CANCELED")
			elif key == "canceled_bids":
				got[key] = sorted(r["bid_amount"] for r in wv if r["status"] == "CANCELED")
			elif key == "failed":
				got[key] = sum(1 for r in wv if r["status"].startswith("FAILED"))
			elif key == "freeagent_rows":
				got[key] = sum(1 for r in pr if r["type"] == "FREEAGENT")
			elif key == "roster_drops":
				got[key] = sum(1 for r in pr if r["type"] == "ROSTER")
			elif key == "skipped_FUTURE_ROSTER":
				got[key] = None  # filled below from per-period type counts
			elif key == "skipped_isPending":
				got[key] = stats["skipped_isPending_by_period"][period]
			elif key == "executed_on":
				c = Counter(utc_date(r["processed_at"]) for r in ex)
				got[key] = {d: c.get(d, 0) for d in exp[key]}
			elif key == "status_on":
				got[key] = {(d, s): sum(1 for r in wv if utc_date(r["processed_at"]) == d and r["status"] == s)
				            for (d, s) in exp[key]}
			elif key == "executed_team_bids":
				got[key] = sorted((r["espn_team_id"], r["bid_amount"]) for r in ex)
			elif key == "status_counts":
				got[key] = {s: sum(1 for r in wv if r["status"] == s) for s in exp[key]}
		if "skipped_FUTURE_ROSTER" in exp:
			got["skipped_FUTURE_ROSTER"] = stats["future_roster_by_period"][period]
		for key in exp:
			mark = "PASS" if got[key] == exp[key] else "FAIL"
			print(f"    p{period:<2} {key:<22} {mark}  expected {exp[key]}  got {got[key]}")


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
async def write_rows(conn, season, rows):
	"""Upsert each transaction on espn_txn_id and replace its items, all in one DB transaction."""
	ins = upd = n_items = 0
	async with conn.transaction():
		for r in rows:
			res = await conn.fetchrow("""
				INSERT INTO transactions (espn_txn_id, espn_related_txn_id, cancel_twin_of, season, week, team_id,
				                          type, status, bid_amount, proposed_at, processed_at)
				VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
				ON CONFLICT (espn_txn_id) DO UPDATE SET
					espn_related_txn_id = EXCLUDED.espn_related_txn_id,
					cancel_twin_of = EXCLUDED.cancel_twin_of,
					season = EXCLUDED.season, week = EXCLUDED.week, team_id = EXCLUDED.team_id,
					type = EXCLUDED.type, status = EXCLUDED.status, bid_amount = EXCLUDED.bid_amount,
					proposed_at = EXCLUDED.proposed_at, processed_at = EXCLUDED.processed_at
				RETURNING id, (xmax = 0) AS inserted
			""", r["espn_txn_id"], r["espn_related_txn_id"], r["cancel_twin_of"], dec(season), r["week"],
			     r["team_id"], r["type"], r["status"], r["bid_amount"], r["proposed_at"], r["processed_at"])
			if res["inserted"]:
				ins += 1
			else:
				upd += 1
				await conn.execute("DELETE FROM transaction_items WHERE transaction_id = $1", res["id"])
			await conn.executemany("""
				INSERT INTO transaction_items (transaction_id, item_type, espn_player_id, player_name, position,
				                               from_team_id, to_team_id)
				VALUES ($1, $2, $3, $4, $5, $6, $7)
			""", [(res["id"], i["item_type"], i["espn_player_id"], i["player_name"], i["position"],
			       i["from_team_id"], i["to_team_id"]) for i in r["items"]])
			n_items += len(r["items"])
	print(f"[write] {season}: transactions inserted {ins}, updated {upd}; transaction_items written {n_items}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
async def run(seasons, dry_run):
	sess = espn_session()
	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	totals = []
	try:
		print(f"=== ESPN transactions import {seasons} — {'DRY RUN (no DB writes)' if dry_run else 'LIVE'} ===")
		for season in seasons:
			try:
				rows, stats = await import_season(conn, sess, season, dry_run)
				ty = Counter(r["type"] for r in rows)
				totals.append((season, len(rows), ty["WAIVER"], ty["FREEAGENT"], ty["ROSTER"],
				               sum(1 for r in rows if r["status"] == "FAILED_INVALIDPLAYERSOURCE"),
				               len(stats["unresolved_players"])))
			except SeasonStop as e:
				print(f"!!! STOPPED season {season}: {e}")
				totals.append((season, "STOPPED", "", "", "", "", ""))
	finally:
		await conn.close()
	print("\n=== SUMMARY ===")
	print("season | kept | WAIVER | FREEAGENT | ROSTER drops | FAILED_INVALIDPLAYERSOURCE | unresolved players")
	for t in totals:
		print(" | ".join(str(x) for x in t))
	if dry_run:
		print("\n(DRY RUN — nothing was written to the database.)")


def main():
	ap = argparse.ArgumentParser(description="Import ESPN transaction history (2018+) into Rockwood.")
	g = ap.add_mutually_exclusive_group(required=True)
	g.add_argument("--season", type=int, help="season year, e.g. 2025")
	g.add_argument("--all", action="store_true", help=f"all seasons {SEASONS[0]}-{SEASONS[-1]}")
	ap.add_argument("--dry-run", action="store_true", help="fetch and summarise without writing to the DB")
	args = ap.parse_args()
	if args.season is not None and args.season < 2018:
		sys.exit("ERROR: ESPN does not serve transactions before 2018.")
	asyncio.run(run(SEASONS if args.all else [args.season], args.dry_run))


if __name__ == "__main__":
	main()
