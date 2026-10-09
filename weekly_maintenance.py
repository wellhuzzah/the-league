#!/usr/bin/env python
"""
weekly_maintenance.py - weekly ESPN upkeep, run by Task Scheduler on Wednesday mornings.

Steps, in order, each independent (a failure is logged and the next step still runs):
	feed     activity_feed_archive.py (snapshot + D: copy + GitHub push)
	scores   espn_import.py --season S             (matchups, box_scores, draft_picks)
	waivers  espn_transactions_import.py --season S (transactions, transaction_items)

Each import first runs with --dry-run; its output is checked against guardrails (below) and the
database, and only if every check passes does the live run happen. A tripped guardrail means
nothing is written for that step. After a live run the database is checked against the dry run.

Outside the season (February-August) the imports are skipped; the feed step keeps running until
ESPN removes the feed (activity_feed_archive.py exit 3), which is then logged as information.

One line per step per run goes to weekly_maintenance.log (gitignored). Each step's full output is
kept in maintenance_output/ (gitignored) for when a guardrail trips.

Exit code: 0 if every step passed or was skipped; otherwise the sum of 1 (feed), 2 (scores),
4 (waivers) for the steps that failed.
"""

import asyncio
import glob
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import date, datetime, timezone

import asyncpg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from espn_raw import RAW_DIR, load_archived  # noqa: E402

PYTHON = sys.executable
LOG = os.path.join(HERE, "weekly_maintenance.log")
OUTPUT_DIR = os.path.join(HERE, "maintenance_output")
STEP_BITS = {"feed": 1, "scores": 2, "waivers": 4}

REG_SEASON_WEEKS = 13          # weeks 14+ are playoffs (matches espn_import.py)
STARTERS_PER_TEAM = 9
MAX_NEW_WEEKS = 2              # one new week normally; two covers one missed run
ROWS_TOLERANCE = 0.15          # new week's box_scores rows vs the median stored week
MAX_POINT_CORRECTIONS = 40     # stat corrections allowed, and only in the latest stored week
MAX_NEW_TRANSACTIONS = 150     # busiest week on record is 47


def current_season(today):
	return today.year if today.month >= 8 else today.year - 1


def in_season(today, season):
	# Kickoff is early September; the championship week ends in early January
	return date(season, 9, 1) <= today <= date(season + 1, 1, 20)


def log(step, status, detail):
	stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
	line = f"{stamp} {step:<7} {status:<5} {detail}"
	with open(LOG, "a", encoding="ascii", errors="replace") as f:
		f.write(line + "\n")
	print(line)


def run(step, label, args, timeout=1800):
	"""Run a script with this interpreter; keep its full output in maintenance_output/."""
	env = dict(os.environ, PYTHONIOENCODING="utf-8")
	r = subprocess.run([PYTHON, *args], cwd=HERE, capture_output=True, text=True,
	                   encoding="utf-8", errors="replace", timeout=timeout, env=env)
	os.makedirs(OUTPUT_DIR, exist_ok=True)
	stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	with open(os.path.join(OUTPUT_DIR, f"{stamp}_{step}_{label}.txt"), "w", encoding="utf-8") as f:
		f.write(f"$ {' '.join(args)}\nexit={r.returncode}\n\n{r.stdout}\n--- stderr ---\n{r.stderr}")
	return r.returncode, r.stdout + r.stderr


def last_line(text):
	lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
	return lines[-1][:200] if lines else "(no output)"


async def db_fetch(sql, *args):
	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	try:
		return await conn.fetch(sql, *args)
	finally:
		await conn.close()


def query(sql, *args):
	return asyncio.run(db_fetch(sql, *args))


def latest_league_status(season):
	"""status block of the newest archived matchup response (the dry run just fetched one)."""
	files = sorted(glob.glob(os.path.join(RAW_DIR, str(season), "mMatchup+mMatchupScore", "*", "*_200.json.gz")))
	if not files:
		return {}
	return json.loads(load_archived(files[-1])).get("status", {})


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def step_feed(season, today):
	code, out = run("feed", "run", ["activity_feed_archive.py"])
	if code == 0:
		summary = next((l for l in out.splitlines() if l.startswith("messages:")), last_line(out))
		log("feed", "ok", summary[:200])
		return True
	if code == 3 and not in_season(today, season):
		log("feed", "info", "feed gone after the season; rollover has happened, nothing to archive")
		return True
	what = {2: "ESPN error", 3: "feed gone DURING the season", 4: "snapshot ok, backup failed"}.get(code, "failed")
	log("feed", "FAIL", f"exit={code} {what}: {last_line(out)}")
	return False


def step_scores(season):
	teams = query("SELECT COUNT(*) n FROM espn_team_map WHERE season = $1", season)[0]["n"]
	stored = {r["week"]: r for r in query("""
		SELECT m.week, COUNT(DISTINCT m.id) matchups, COUNT(b.id) box_rows
		FROM matchups m LEFT JOIN box_scores b ON b.matchup_id = m.id
		WHERE m.season = $1 GROUP BY m.week""", season)}
	drafted = query("SELECT COUNT(*) n FROM draft_picks WHERE season = $1", season)[0]["n"]
	db_max = max(stored, default=0)

	code, out = run("scores", "dry", ["espn_import.py", "--season", str(season), "--dry-run"])
	if code:
		log("scores", "FAIL", f"dry run exit={code} (ESPN or import error): {last_line(out)}")
		return False
	try:
		summary = json.loads(out.split("=== SUMMARY ===", 1)[1].split("\n\n", 1)[0])
	except (IndexError, ValueError):
		log("scores", "FAIL", "guardrail: could not read the dry run summary; nothing written")
		return False

	problems = []
	if "=== WARNINGS" in out:
		problems.append("dry run reported warnings")
	if teams and summary.get("espn_team_map") != teams:
		problems.append(f"team map {summary.get('espn_team_map')} teams, expected {teams}")
	per_week = {int(w): n for w, n in summary["matchups"]["per_week"].items()}
	box = {int(w): v for w, v in summary["box_scores"]["per_week"].items()}
	weeks = sorted(per_week)
	new_max = max(weeks, default=0)
	if weeks != list(range(1, new_max + 1)):
		problems.append(f"completed weeks not contiguous: {weeks}")
	if new_max < db_max:
		problems.append(f"ESPN has fewer completed weeks ({new_max}) than stored ({db_max})")
	if new_max - db_max > MAX_NEW_WEEKS:
		problems.append(f"{new_max - db_max} new weeks at once (stored {db_max}, ESPN {new_max})")
	st = latest_league_status(season)
	latest, final = st.get("latestScoringPeriod"), st.get("finalScoringPeriod")
	if latest and final and new_max >= latest and latest <= final:
		problems.append(f"week {new_max} marked complete but the current scoring period is {latest}")
	reg_rows = [stored[w]["box_rows"] for w in stored if w <= REG_SEASON_WEEKS]
	median = statistics.median(reg_rows) if len(reg_rows) >= 2 else None
	for w in weeks:
		starters = box.get(w, {}).get("starters", 0)
		rows = starters + box.get(w, {}).get("bench", 0) + box.get(w, {}).get("ir", 0)
		if w <= REG_SEASON_WEEKS:
			if teams and per_week[w] != teams // 2:
				problems.append(f"wk{w}: {per_week[w]} matchups, expected {teams // 2}")
			if teams and starters < STARTERS_PER_TEAM * teams - 3:
				problems.append(f"wk{w}: {starters} starters, expected about {STARTERS_PER_TEAM * teams}")
			if w > db_max and median and abs(rows - median) > ROWS_TOLERANCE * median:
				problems.append(f"wk{w}: {rows} box_scores rows vs stored median {median:.0f}")
		elif teams and not 1 <= per_week[w] <= teams // 2:
			problems.append(f"wk{w} (playoffs): {per_week[w]} matchups")
	bs = summary["box_scores"]
	if bs["existing_rows_not_returned"]:
		problems.append(f"{bs['existing_rows_not_returned']} stored box_scores rows missing from ESPN")
	diffs = bs["diff_counts"]
	if diffs["starter_now_bench"] or diffs["new_starter_rows"]:
		problems.append(f"lineup changed after the fact: {diffs['starter_now_bench']} starter->bench, "
		                f"{diffs['new_starter_rows']} new starter rows")
	if diffs["points"]:
		corrected = {int(m) for m in re.findall(r"^    wk(\d+) .*: \S+ -> \S+$", out.split("[box_scores] points", 1)[-1], re.M)}
		if diffs["points"] > MAX_POINT_CORRECTIONS or any(w < db_max for w in corrected):
			problems.append(f"{diffs['points']} point changes in weeks {sorted(corrected)} (only the latest stored week may change)")
	if drafted and summary.get("draft_picks", {}).get("count_seen") != drafted:
		problems.append(f"draft: ESPN {summary.get('draft_picks', {}).get('count_seen')} picks, stored {drafted}")
	if problems:
		log("scores", "FAIL", "guardrail: " + "; ".join(problems) + "; nothing written")
		return False

	code, out = run("scores", "live", ["espn_import.py", "--season", str(season)])
	if code:
		log("scores", "FAIL", f"live run exit={code}: {last_line(out)}")
		return False
	after = {r["week"]: r for r in query("""
		SELECT m.week, COUNT(DISTINCT m.id) matchups, COUNT(b.id) box_rows
		FROM matchups m LEFT JOIN box_scores b ON b.matchup_id = m.id
		WHERE m.season = $1 GROUP BY m.week""", season)}
	mismatch = [w for w in weeks if after.get(w, {}).get("matchups") != per_week[w]]
	rows_after = sum(r["box_rows"] for r in after.values())
	if mismatch or rows_after != bs["rows_seen"]:
		log("scores", "FAIL", f"post-check: matchups differ in weeks {mismatch}; box_scores {rows_after} vs dry run {bs['rows_seen']}")
		return False
	log("scores", "ok", f"weeks 1-{new_max} (+{new_max - db_max}) | matchups {sum(per_week.values())} | "
	                    f"box_scores {rows_after} (+{rows_after - sum(r['box_rows'] for r in stored.values())})")
	return True


def step_waivers(season):
	stored = query("SELECT COUNT(*) n, MAX(week) wk FROM transactions WHERE season = $1", season)[0]
	db_rows, db_week = stored["n"], stored["wk"] or 0

	code, out = run("waivers", "dry", ["espn_transactions_import.py", "--season", str(season), "--dry-run"])
	if code or "!!! STOPPED" in out:
		log("waivers", "FAIL", f"dry run exit={code} (ESPN or import error): {last_line(out)}")
		return False

	def num(pattern):
		m = re.search(pattern, out, re.M)
		return int(m.group(1)) if m else None
	kept = num(r"^\[kept\] (\d+) rows")
	latest = num(r"scoringPeriodId 0\.\.(\d+)")
	unresolved = num(r"unresolved player ids[^:]*: (\d+)")
	mismatched = num(r"mismatched dates: (\d+)")
	faab = re.search(r"WAIVER only (\d+)/(\d+)", out)
	truncation = re.search(r"^\[truncation\].*-> (\w+)", out, re.M)

	problems = []
	if None in (kept, latest, unresolved, mismatched) or not faab or not truncation:
		problems.append("could not read the dry run checks")
	else:
		if kept < db_rows:
			problems.append(f"ESPN returns {kept} rows, fewer than the {db_rows} stored")
		if kept - db_rows > MAX_NEW_TRANSACTIONS:
			problems.append(f"{kept - db_rows} new rows at once (limit {MAX_NEW_TRANSACTIONS})")
		if latest < db_week:
			problems.append(f"ESPN's latest period {latest} is before stored week {db_week}")
		if unresolved:
			problems.append(f"{unresolved} unresolved player ids")
		if mismatched:
			problems.append(f"executed waiver claims disagree with ESPN on {mismatched} date(s)")
		if faab.group(1) != faab.group(2):
			problems.append(f"FAAB spent matches ESPN for only {faab.group(1)}/{faab.group(2)} teams")
		if truncation.group(1) != "OK":
			problems.append("draft-row truncation check failed")
	if problems:
		log("waivers", "FAIL", "guardrail: " + "; ".join(problems) + "; nothing written")
		return False

	code, out = run("waivers", "live", ["espn_transactions_import.py", "--season", str(season)])
	if code or "!!! STOPPED" in out:
		log("waivers", "FAIL", f"live run exit={code}: {last_line(out)}")
		return False
	after = query("SELECT COUNT(*) n FROM transactions WHERE season = $1", season)[0]["n"]
	if after != kept:
		log("waivers", "FAIL", f"post-check: {after} rows stored vs {kept} in the dry run")
		return False
	log("waivers", "ok", f"periods 0-{latest} | transactions {after} (+{after - db_rows})")
	return True


def main():
	today = datetime.now().date()
	season = current_season(today)
	failed = []
	steps = [("feed", lambda: step_feed(season, today))]
	if in_season(today, season):
		steps += [("scores", lambda: step_scores(season)), ("waivers", lambda: step_waivers(season))]
	else:
		log("scores", "skip", f"off-season ({season} season)")
		log("waivers", "skip", f"off-season ({season} season)")
	for name, fn in steps:
		try:
			ok = fn()
		except Exception as e:    # a crash in one step never stops the next
			log(name, "FAIL", f"crashed: {type(e).__name__}: {e}"[:300])
			ok = False
		if not ok:
			failed.append(name)
	return sum(STEP_BITS[s] for s in failed)


if __name__ == "__main__":
	sys.exit(main())
