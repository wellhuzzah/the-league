#!/usr/bin/env python
"""
weekly_maintenance.py - weekly ESPN upkeep, run by Task Scheduler on Thursday mornings.

Steps, in order, each independent (a failure is logged and the next step still runs):
	feed     activity_feed_archive.py (snapshot + D: copy + GitHub push)
	scores   espn_import.py --season S             (matchups, box_scores, draft_picks)
	waivers  espn_transactions_import.py --season S (transactions, transaction_items)
	backup   nightly_backup.py --push   (fresh pg_dump; redacted SQL copy to rockwood-archive)
	analyze  SELECT rockwood_analyze()  (planner statistics; the function runs ANALYZE as its
	         owner, postgres, since rockwood_writer can't ANALYZE tables it doesn't own on PG 16)

Each import first runs with --dry-run; its output is checked against guardrails (below) and the
database, and only if every check passes does the live run happen. A tripped guardrail means
nothing is written for that step. After a live run the database is checked against the dry run.
Two real league events are allowed through rather than tripping (both are listed on the step's
log line, and a new one gets a one-line heads-up post):
	scores   an empty starting slot (fewer than 9 starters) whose points still add up to ESPN's
	waivers  FAAB moved in a completed trade: the archived activity feed (type 290 messages) plus
	         faab_adjustments.json must explain ESPN's spent budget exactly, for every team
The checks themselves are pure functions (check_scores, check_waivers), tested offline by
tests/test_guardrails.py.

Outside the season (February-August) the imports are skipped; the feed step keeps running until
ESPN removes the feed (activity_feed_archive.py exit 3), which is then logged as information.

One line per step per run goes to weekly_maintenance.log (gitignored). Each step's full output is
kept in maintenance_output/ (gitignored) for when a guardrail trips.

Exit code: 0 if every step passed or was skipped; otherwise the sum of 1 (feed), 2 (scores),
4 (waivers), 8 (backup), 16 (analyze) for the steps that failed.

Notifications (optional, set in .env; nothing is sent when unset):
	NOTIFY_WEBHOOK_URL  Discord or Slack incoming webhook (or an ntfy topic URL). Posted when a
	                    step fails, or when the last fully successful run is over STALE_DAYS old.
	                    A clean run that let a new empty slot or trade FAAB through posts one line.
	HEALTHCHECK_URL     healthchecks.io-style ping URL: pinged on success, URL + "/fail" on
	                    failure. Its own schedule alerts you if the task stops running at all.
	Message text is this run's log lines with every .env value replaced by [redacted].
weekly_maintenance.last_success (gitignored) holds the time of the last fully successful run.

Manual override, interactive use only (see docs/weekly_maintenance_runbook.md):
	python weekly_maintenance.py --accept-diffs scores [--accept-diffs waivers]
A tripped guardrail for that step is shown and needs "ACCEPT" typed at the console; without a
console (Task Scheduler) the override is refused. Unreadable dry runs and ESPN errors can never be
overridden.
"""

import argparse
import asyncio
import glob
import json
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import asyncpg
import requests
from dotenv import dotenv_values, load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from espn_raw import RAW_DIR, load_archived  # noqa: E402

PYTHON = sys.executable
LOG = os.path.join(HERE, "weekly_maintenance.log")
OUTPUT_DIR = os.path.join(HERE, "maintenance_output")
STEP_BITS = {"feed": 1, "scores": 2, "waivers": 4, "backup": 8, "analyze": 16}

REG_SEASON_WEEKS = 13          # weeks 14+ are playoffs (matches espn_import.py)
STARTERS_PER_TEAM = 9
MAX_NEW_WEEKS = 2              # one new week normally; two covers one missed run
ROWS_TOLERANCE = 0.15          # new week's box_scores rows vs the median stored week
MAX_POINT_CORRECTIONS = 40     # stat corrections allowed, and only in the latest stored week
MAX_NEW_TRANSACTIONS = 150     # busiest week on record is 47

# The only importer warning the scores step lets through (espn_import.py, box scores)
EMPTY_SLOT = re.compile(r"box wk(\d+) team (\d+): (\d+) starters \(expected 9\) \u2014 empty lineup slot\(s\)\.")
STARTER_SUM = re.compile(r"box wk(\d+) team (\d+): starter sum ")
# FAAB lines of espn_transactions_import.py's report
FAAB_HEAD = re.compile(r"WAIVER only (\d+)/(\d+)")
FAAB_LINE = re.compile(r"^    ESPN team (-?\d+) \(team_id [^)]*\): spent (\S+); WAIVER (\d+)( X)?; WAIVER\+FA ", re.M)
FEED_DIR = os.path.join(RAW_DIR, "activity_feed")
FEED_TRADE_COMPLETED = 244     # activity feed: one message per player moved, when a trade goes through
FEED_TRADE_FAAB = 290          # FAAB in a completed trade: from = sender, to = receiver, targetId = dollars
FAAB_ADJUSTMENTS = os.path.join(HERE, "faab_adjustments.json")   # trades the feed doesn't show; see the runbook

LAST_SUCCESS = os.path.join(HERE, "weekly_maintenance.last_success")
STALE_DAYS = 8                 # weekly task: more than this since a clean run means it stopped
RUN_LINES = []                 # this run's log lines, for the notification
HEADS_UP = []                  # events let through for the first time this run, for a one-line post


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
	RUN_LINES.append(line)
	print(line)


def interactive_console():
	"""True only for a real console. On Windows isatty() is also True for NUL, which is what a
	scheduled task can get as stdin, so ask Windows for the console mode as well."""
	if sys.stdin is None or not sys.stdin.isatty():
		return False
	if os.name != "nt":
		return True
	import ctypes
	import msvcrt
	mode = ctypes.c_uint32()
	handle = msvcrt.get_osfhandle(sys.stdin.fileno())
	return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def guardrail_tripped(step, problems, accept):
	"""True if the step must stop. An interactive operator can accept the problems for this run."""
	if not problems:
		return False
	if step in accept:
		if not interactive_console():
			print(f"--accept-diffs {step} refused: no interactive console")
		else:
			print(f"\n{step} guardrails tripped:")
			for p in problems:
				print("   - " + p)
			try:
				answer = input(f"Type ACCEPT to run the live {step} import anyway: ").strip()
			except EOFError:
				answer = ""
			if answer == "ACCEPT":
				log(step, "warn", "guardrail overridden by operator: " + "; ".join(problems))
				return False
	log(step, "FAIL", "guardrail: " + "; ".join(problems) + "; nothing written")
	return True


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


def feed_faab_trades(season, feed_dir=FEED_DIR):
	"""FAAB moved in completed trades, from the archived activity feed (activity_feed_archive.py):
	{message id: (from ESPN team, to ESPN team, dollars, date ms)}. Reads every complete snapshot of
	the season (all pages HTTP 200), newest copy of each topic winning, so a failed feed step today
	still leaves the earlier snapshots, and nothing is lost when ESPN deletes the feed at rollover.
	A type 290 message counts only in a topic that also has a 244 (trade completed); 284, the same
	FAAB when the trade is accepted and can still be vetoed, is ignored. None if no snapshot reads."""
	runs = defaultdict(list)
	for path in glob.glob(os.path.join(feed_dir, str(season), "*_p*_*.json.gz")):
		stamp, _, status = os.path.basename(path)[:-len(".json.gz")].rsplit("_", 2)
		runs[stamp].append((status, path))
	topics, complete = {}, 0
	for stamp in sorted(runs):
		if any(status != "200" for status, _ in runs[stamp]):
			continue    # an ESPN error or a gone feed partway through that run
		try:
			pages = [json.loads(load_archived(path))["topics"] for _, path in sorted(runs[stamp])]
			for t in (t for page in pages for t in page):
				topics[t["id"]] = t
		except (OSError, EOFError, ValueError, KeyError, TypeError):
			continue
		complete += 1
	if not complete:
		return None
	trades = {}
	for t in topics.values():
		messages = t.get("messages") or []
		if not any(m.get("messageTypeId") == FEED_TRADE_COMPLETED for m in messages):
			continue
		for m in messages:
			f, to, amount = m.get("from"), m.get("to"), m.get("targetId")
			if (m.get("messageTypeId") == FEED_TRADE_FAAB and all(type(v) is int for v in (f, to, amount))
			        and amount > 0 and f != to):
				trades[m.get("id")] = (f, to, amount, m.get("date") or 0)
	return trades


def last_ok(step, path=LOG):
	"""UTC time of the newest '<step> ok' line in the log, or None."""
	try:
		with open(path, encoding="ascii", errors="replace") as f:
			stamps = [l.split()[0] for l in f if l.split()[1:3] == [step, "ok"]]
	except OSError:
		return None
	return datetime.strptime(stamps[-1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if stamps else None


# --------------------------------------------------------------------------- #
# guardrail checks: pure functions of a dry run's output and the stored state (no DB, network or
# files), so tests/test_guardrails.py can replay captured dry runs offline
# --------------------------------------------------------------------------- #
class Unreadable(Exception):
	"""A dry run's output (or faab_adjustments.json) can't be read. Never overridable."""


def importer_warnings(out):
	"""Items of espn_import.py's WARNINGS block ([] when it has none). Unreadable unless the block is
	exactly the header's count of '  - ' lines, followed by a blank line."""
	if "=== WARNINGS" not in out:
		return []
	m = re.search(r"^=== WARNINGS \((\d+)\) ===\n", out, re.M)
	if not m or out.count("=== WARNINGS") != 1:
		raise Unreadable("could not read the dry run warnings")
	block = out[m.end():].split("\n\n", 1)
	lines = block[0].splitlines()
	if len(block) != 2 or len(lines) != int(m.group(1)) or not all(l.startswith("  - ") for l in lines):
		raise Unreadable("could not read the dry run warnings")
	return [l[4:] for l in lines]


def split_warnings(warnings):
	"""(tolerated, fatal). Tolerated: an empty starting slot (fewer than 9 starters) in a team-week
	with no starter-sum warning, so ESPN's total is exactly what was started. tolerated holds
	(week, team, starters); fatal is every other warning, as printed."""
	sums = {m.group(1, 2) for m in map(STARTER_SUM.match, warnings) if m}
	tolerated, fatal = [], []
	for w in warnings:
		m = EMPTY_SLOT.fullmatch(w)
		if m and int(m.group(3)) < STARTERS_PER_TEAM and m.group(1, 2) not in sums:
			tolerated.append(tuple(map(int, m.groups())))
		else:
			fatal.append(w)
	return tolerated, fatal


def check_scores(out, teams, stored, drafted, st):
	"""(problems, info) for a scores dry run. stored: {week: {"matchups", "box_rows"}} from the DB;
	st: ESPN's league status block. info carries what the live run's post-check and log line need."""
	db_max = max(stored, default=0)
	try:
		summary = json.loads(out.split("=== SUMMARY ===", 1)[1].split("\n\n", 1)[0])
	except (IndexError, ValueError):
		raise Unreadable("could not read the dry run summary")
	tolerated, fatal = split_warnings(importer_warnings(out))
	explained = defaultdict(int)    # empty starting slots per week that a tolerated warning accounts for
	for w, _, k in tolerated:
		explained[w] += STARTERS_PER_TEAM - k

	problems = []
	if fatal:
		problems.append("dry run warnings: " + "; ".join(fatal[:3]) + (f" (+{len(fatal) - 3} more)" if len(fatal) > 3 else ""))
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
			if teams and starters + explained[w] < STARTERS_PER_TEAM * teams - 3:
				problems.append(f"wk{w}: {starters} starters ({explained[w]} empty slots explained), "
				                f"expected about {STARTERS_PER_TEAM * teams}")
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
	return problems, {"per_week": per_week, "weeks": weeks, "new_max": new_max, "db_max": db_max,
	                  "rows_seen": bs["rows_seen"],
	                  "slots_new": [s for s in tolerated if s[0] > db_max],
	                  "slots_stored": len([s for s in tolerated if s[0] <= db_max])}


def faab_adjustments(season, path=FAAB_ADJUSTMENTS):
	"""[(from ESPN team, to ESPN team, dollars, note)]: FAAB moved in trades that the archived feed
	doesn't show, kept by hand in faab_adjustments.json. No file means none; malformed is Unreadable."""
	try:
		with open(path, encoding="utf-8") as f:
			entries = json.load(f)
	except FileNotFoundError:
		return []
	except (OSError, ValueError) as e:
		raise Unreadable(f"could not read faab_adjustments.json ({type(e).__name__})")
	if not isinstance(entries, list):
		raise Unreadable("faab_adjustments.json is not a list")
	out = []
	for i, e in enumerate(entries):
		ok = (isinstance(e, dict)
		      and all(type(e.get(k)) is int for k in ("season", "from_espn_team", "to_espn_team", "amount"))
		      and e["amount"] > 0 and e["from_espn_team"] != e["to_espn_team"]
		      and isinstance(e.get("note"), str) and e["note"].strip())
		if not ok:
			raise Unreadable(f"faab_adjustments.json entry {i} is malformed")
		if e["season"] == season:
			out.append((e["from_espn_team"], e["to_espn_team"], e["amount"], e["note"]))
	return out


def faab_problems(out, feed, manual):
	"""Every team: ESPN's acquisitionBudgetSpent == its executed WAIVER bids + the net FAAB it sent in
	trades (feed: feed_faab_trades(), None if unreadable; manual: faab_adjustments()). The importer
	lists only the teams whose spent differs from their bids, so an unlisted team's spent equals its
	bids, and a trade that names it can't be right."""
	head = FAAB_HEAD.search(out)
	lines = FAAB_LINE.findall(out)
	if not head or int(head.group(2)) - int(head.group(1)) != sum(1 for *_, x in lines if x):
		raise Unreadable("could not read the dry run checks")
	sent = defaultdict(int)
	for f, to, amount, _ in list((feed or {}).values()) + list(manual):
		sent[f] += amount
		sent[to] -= amount
	problems, off = [], {}
	for team, spent, bids, _ in lines:
		if not spent.isdigit():
			problems.append(f"ESPN team {team} has no FAAB spent figure ({spent})")
			off[int(team)] = 0    # already a problem; don't also report it as a mismatch
			continue
		off[int(team)] = int(spent) - int(bids) - sent.get(int(team), 0)
	for team, n in sent.items():
		off.setdefault(team, -n)
	off = {t: d for t, d in off.items() if d}
	if off:
		n = int(head.group(2))
		msg = (f"FAAB spent matches ESPN for only {n - len(off)}/{n} teams after trades "
		       f"(ESPN minus expected: {', '.join(f'team {t} {d:+}' for t, d in sorted(off.items()))})")
		pairs = [(a, b, d) for a, d in sorted(off.items()) for b, e in sorted(off.items()) if d > 0 and e == -d]
		if pairs:
			msg += ("; looks like " + ", ".join(f"${d} from ESPN team {a} to ESPN team {b}" for a, b, d in pairs)
			        + " moved in a trade that neither the feed nor faab_adjustments.json shows")
		if feed is None:
			msg += "; no archived feed snapshot could be read"
		problems.append(msg)
	return problems


def check_waivers(out, db_rows, db_week, feed, manual):
	"""(problems, info) for a waivers dry run. feed and manual: FAAB moved in trades (see faab_problems)."""
	def num(pattern):
		m = re.search(pattern, out, re.M)
		return int(m.group(1)) if m else None
	kept = num(r"^\[kept\] (\d+) rows")
	latest = num(r"scoringPeriodId 0\.\.(\d+)")
	unresolved = num(r"unresolved player ids[^:]*: (\d+)")
	mismatched = num(r"mismatched dates: (\d+)")
	truncation = re.search(r"^\[truncation\].*-> (\w+)", out, re.M)

	if None in (kept, latest, unresolved, mismatched) or not truncation:
		raise Unreadable("could not read the dry run checks")
	faab = faab_problems(out, feed, manual)
	problems = []
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
	problems += faab
	if truncation.group(1) != "OK":
		problems.append("draft-row truncation check failed")
	return problems, {"kept": kept, "latest": latest, "trades": feed or {}, "manual": manual}


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


def step_scores(season, accept):
	teams = query("SELECT COUNT(*) n FROM espn_team_map WHERE season = $1", season)[0]["n"]
	stored = {r["week"]: r for r in query("""
		SELECT m.week, COUNT(DISTINCT m.id) matchups, COUNT(b.id) box_rows
		FROM matchups m LEFT JOIN box_scores b ON b.matchup_id = m.id
		WHERE m.season = $1 GROUP BY m.week""", season)}
	drafted = query("SELECT COUNT(*) n FROM draft_picks WHERE season = $1", season)[0]["n"]

	code, out = run("scores", "dry", ["espn_import.py", "--season", str(season), "--dry-run"])
	if code:
		log("scores", "FAIL", f"dry run exit={code} (ESPN or import error): {last_line(out)}")
		return False
	try:
		problems, info = check_scores(out, teams, stored, drafted, latest_league_status(season))
	except Unreadable as e:
		log("scores", "FAIL", f"guardrail: {e}; nothing written")
		return False
	if guardrail_tripped("scores", problems, accept):
		return False

	weeks, per_week, new_max, db_max = info["weeks"], info["per_week"], info["new_max"], info["db_max"]
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
	if mismatch or rows_after != info["rows_seen"]:
		log("scores", "FAIL", f"post-check: matchups differ in weeks {mismatch}; box_scores {rows_after} vs dry run {info['rows_seen']}")
		return False
	new = [f"wk{w} team {t} ({k} starters)" for w, t, k in info["slots_new"]]
	note = ""
	if new or info["slots_stored"]:
		note = (" | empty lineup slots: " + (", ".join(new) or "none new")
		        + (f" (+{info['slots_stored']} in stored weeks)" if info["slots_stored"] else ""))
	log("scores", "ok", f"weeks 1-{new_max} (+{new_max - db_max}) | matchups {sum(per_week.values())} | "
	                    f"box_scores {rows_after} (+{rows_after - sum(r['box_rows'] for r in stored.values())})" + note)
	if new:
		HEADS_UP.append("empty lineup slot " + ", ".join(new))
	return True


def step_waivers(season, accept):
	stored = query("SELECT COUNT(*) n, MAX(week) wk FROM transactions WHERE season = $1", season)[0]
	db_rows, db_week = stored["n"], stored["wk"] or 0

	code, out = run("waivers", "dry", ["espn_transactions_import.py", "--season", str(season), "--dry-run"])
	if code or "!!! STOPPED" in out:
		log("waivers", "FAIL", f"dry run exit={code} (ESPN or import error): {last_line(out)}")
		return False
	try:
		problems, info = check_waivers(out, db_rows, db_week, feed_faab_trades(season), faab_adjustments(season))
	except Unreadable as e:
		log("waivers", "FAIL", f"guardrail: {e}; nothing written")
		return False
	if guardrail_tripped("waivers", problems, accept):
		return False

	kept, latest = info["kept"], info["latest"]
	code, out = run("waivers", "live", ["espn_transactions_import.py", "--season", str(season)])
	if code or "!!! STOPPED" in out:
		log("waivers", "FAIL", f"live run exit={code}: {last_line(out)}")
		return False
	after = query("SELECT COUNT(*) n FROM transactions WHERE season = $1", season)[0]["n"]
	if after != kept:
		log("waivers", "FAIL", f"post-check: {after} rows stored vs {kept} in the dry run")
		return False
	since = last_ok("waivers")
	trades, new = [], []
	for f, to, amount, when in sorted(info["trades"].values(), key=lambda t: t[3]):
		fresh = since is None or when > since.timestamp() * 1000
		trades.append(f"${amount} ESPN team {f} -> {to}" + (" (new)" if fresh else ""))
		if fresh:
			new.append(f"${amount} ESPN team {f} -> {to}")
	note = (f" | FAAB in trades: {', '.join(trades)}" if trades else "") + (
		f" | faab_adjustments.json: {len(info['manual'])} entr{'y' if len(info['manual']) == 1 else 'ies'}" if info["manual"] else "")
	log("waivers", "ok", f"periods 0-{latest} | transactions {after} (+{after - db_rows})" + note)
	if new:
		HEADS_UP.append("FAAB trade matched ESPN: " + ", ".join(new))
	return True


def step_backup():
	code, out = run("backup", "run", ["nightly_backup.py", "--push"])
	if code:
		log("backup", "FAIL", f"exit={code}: {last_line(out)}")
		return False
	push = next((l for l in out.splitlines() if " push " in l), last_line(out))
	log("backup", "ok", push.split(" ok ", 1)[-1].strip()[:200])
	return True


def step_analyze():
	query("SELECT rockwood_analyze()")
	log("analyze", "ok", "planner statistics refreshed")
	return True


# --------------------------------------------------------------------------- #
# notification
# --------------------------------------------------------------------------- #
def days_since_success():
	try:
		with open(LAST_SUCCESS) as f:
			last = datetime.fromisoformat(f.read().strip())
	except (OSError, ValueError):
		return None
	return (datetime.now(timezone.utc) - last).total_seconds() / 86400


def redacted(text):
	for v in dotenv_values(os.path.join(HERE, ".env")).values():
		if v and len(v) >= 6:
			text = re.sub(re.escape(v.strip("{}")), "[redacted]", text, flags=re.IGNORECASE)
	return text


def webhook_body(hook, text):
	if "discord.com" in hook:
		return {"json": {"content": text}}
	if "hooks.slack.com" in hook:
		return {"json": {"text": text}}
	return {"data": text.encode("utf-8")}    # ntfy and similar: plain text


def notify(failed, stale_days):
	ok = not failed
	stale = stale_days is not None and stale_days > STALE_DAYS
	head = (f"Rockwood weekly maintenance: {'ok' if ok else 'FAILED: ' + ', '.join(failed)}"
	        + (f" (last fully successful run was {stale_days:.0f} days ago)" if stale else ""))
	text = redacted(head + "\n" + "\n".join(RUN_LINES))[:1900]
	sends = []
	hc = os.getenv("HEALTHCHECK_URL")
	if hc:
		sends.append(("healthcheck", hc if ok else hc.rstrip("/") + "/fail", {"data": text.encode("utf-8")}))
	hook = os.getenv("NOTIFY_WEBHOOK_URL")
	if hook and (not ok or stale):
		sends.append(("webhook", hook, webhook_body(hook, text)))
	elif hook and HEADS_UP:
		# a clean run that let a real league event through for the first time: one line, no log dump
		line = redacted("Rockwood weekly maintenance: ok, heads-up: " + "; ".join(HEADS_UP))[:1900]
		sends.append(("webhook", hook, webhook_body(hook, line)))
	for name, url, body in sends:
		try:
			requests.post(url, timeout=15, **body).raise_for_status()
		except requests.RequestException as e:
			# never log the URL or the exception text; the URL is itself a secret
			log("notify", "FAIL", f"{name}: {type(e).__name__}")


def main():
	ap = argparse.ArgumentParser(description="Weekly ESPN upkeep (feed snapshot, score and waiver imports).")
	ap.add_argument("--accept-diffs", action="append", default=[], choices=["scores", "waivers"],
	                help="interactive only: allow overriding tripped guardrails for this step")
	accept = set(ap.parse_args().accept_diffs)

	today = datetime.now().date()
	season = current_season(today)
	stale_days = days_since_success()
	failed = []
	steps = [("feed", lambda: step_feed(season, today))]
	if in_season(today, season):
		steps += [("scores", lambda: step_scores(season, accept)), ("waivers", lambda: step_waivers(season, accept))]
	else:
		log("scores", "skip", f"off-season ({season} season)")
		log("waivers", "skip", f"off-season ({season} season)")
	steps += [("backup", step_backup), ("analyze", step_analyze)]    # analyze stays last
	for name, fn in steps:
		try:
			ok = fn()
		except Exception as e:    # a crash in one step never stops the next
			log(name, "FAIL", f"crashed: {type(e).__name__}: {e}"[:300])
			ok = False
		if not ok:
			failed.append(name)
	if not failed:
		with open(LAST_SUCCESS, "w") as f:
			f.write(datetime.now(timezone.utc).isoformat())
	notify(failed, stale_days)
	return sum(STEP_BITS[s] for s in failed)


if __name__ == "__main__":
	sys.exit(main())
