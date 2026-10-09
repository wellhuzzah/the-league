#!/usr/bin/env python
"""
activity_feed_archive.py - snapshot the ESPN league activity feed (kona_league_communication).

The feed is the only ESPN source with trade player lists (message types 244 / 224 / 230), and ESPN
deletes it when the league rolls over to a new season ("This Communication Group does not exist").
Run this daily during the season, plus once after the playoffs, so the feed survives rollover.

Usage:
	python activity_feed_archive.py                 # current season
	python activity_feed_archive.py --season 2026

Each run pages through the whole feed and saves every page's raw response, byte for byte (gzipped),
under espn_raw/activity_feed/{season}/{UTC timestamp}_p{page}_{status}.json.gz. Files are opened in
exclusive-create mode, so an earlier snapshot can never be overwritten. index.json (message id ->
first/last snapshot seen) is rebuilt from all snapshots on every run, so it can't drift from them.
A line per run is appended to runs.log.

After the snapshot is saved and logged, it is backed up (skip with --no-backup):
	1. robocopy of espn_raw/activity_feed/ to LOCAL_BACKUP on the D: drive (raw, never deletes)
	2. a private GitHub repo (wellhuzzah/rockwood-archive, local clone at ARCHIVE_REPO) with the
	   snapshot pages and runs.log only. ESPN responses carry the account's SWID (it is the member
	   id ESPN uses for authors), so the repo copy has it replaced with SWID_PLACEHOLDER, and every
	   staged file is scanned for any .env value before commit; a hit stops the commit and push.
A backup failure gets its own runs.log line and exit code 4, and never touches the snapshot.

No database access. Exit codes: 0 ok, 2 HTTP or parse error, 3 feed gone (season rolled over),
4 snapshot ok but a backup step failed.
"""

import argparse
import glob
import gzip
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from dotenv import dotenv_values, load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
# Scheduled runs (SYSTEM) don't start in this folder, so load .env by path before espn_import reads it
load_dotenv(os.path.join(HERE, ".env"))

from espn_import import BASE_URL, espn_session  # noqa: E402

ARCHIVE_DIR = os.path.join(HERE, "espn_raw", "activity_feed")
LOCAL_BACKUP = r"D:\Backup\rockwood\activity_feed"
ARCHIVE_REPO = r"C:\projects\rockwood-archive"
GIT = r"C:\Program Files\Git\cmd\git.exe"
SWID_PLACEHOLDER = "ARCHIVE-ACCOUNT-SWID"
PAGE_SIZE = 100
MAX_PAGES = 100
DELAY_S = 2.0

# Message types seen in the 2026 feed (meaning inferred from the probe, not documented by ESPN)
MESSAGE_TYPES = {
	244: "trade completed",
	224: "trade accepted",
	230: "trade offer",
	178: "free agent add",
	180: "waiver add",
	179: "drop",
	181: "drop",
	239: "drop",
	188: "lineup move",
}


def current_season(now):
	# The NFL season starts in September; before August the latest league is last year's
	return now.year if now.month >= 8 else now.year - 1


def fetch_page(sess, season, offset):
	filt = {"topics": {
		"filterType": {"value": ["ACTIVITY_TRANSACTIONS"]},
		"limit": PAGE_SIZE,
		"limitPerMessageSet": {"value": 500},
		"offset": offset,
		"sortMessageDate": {"sortPriority": 1, "sortAsc": False},
	}}
	return sess.get(BASE_URL.format(year=season) + "/communication/",
	                params={"view": "kona_league_communication"},
	                headers={"x-fantasy-filter": json.dumps(filt)}, timeout=60)


def save(folder, stamp, page, response):
	path = os.path.join(folder, f"{stamp}_p{page:03d}_{response.status_code}.json.gz")
	with gzip.open(path, "xb") as f:    # "x": never overwrite an earlier snapshot
		f.write(response.content)
	return path


def messages_in(topics):
	for t in topics:
		for m in t.get("messages", []):
			yield t, m


def load_snapshots(folder):
	"""{snapshot stamp: [topics]} for every complete earlier run (all pages returned 200)."""
	runs = {}
	for path in sorted(glob.glob(os.path.join(folder, "*_p*_*.json.gz"))):
		stamp, page, status = os.path.basename(path)[:-len(".json.gz")].rsplit("_", 2)
		runs.setdefault(stamp, {"ok": True, "topics": []})
		if status != "200":
			runs[stamp]["ok"] = False
			continue
		with gzip.open(path, "rb") as f:
			runs[stamp]["topics"].extend(json.loads(f.read()).get("topics", []))
	return {s: r["topics"] for s, r in runs.items() if r["ok"]}


def ms(d):
	return datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if d else "-"


def main():
	ap = argparse.ArgumentParser(description="Snapshot the ESPN league activity feed.")
	ap.add_argument("--season", type=int, default=current_season(datetime.now(timezone.utc)))
	ap.add_argument("--no-backup", action="store_true", help="snapshot only; skip the D: copy and the GitHub push")
	args = ap.parse_args()

	folder = os.path.join(ARCHIVE_DIR, str(args.season))
	os.makedirs(folder, exist_ok=True)
	stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
	code = snapshot(args.season, folder, stamp)
	if args.no_backup:
		return code
	# The snapshot is saved and logged by now; nothing below can change it
	try:
		problems = backup(args.season, folder)
	except Exception as e:
		problems = [f"unexpected {type(e).__name__}: {e}"]
	log(folder, stamp, "backup " + ("ok" if not problems else "FAIL: " + "; ".join(problems)))
	if problems:
		print("BACKUP FAILED:", "; ".join(problems))
	return code or (4 if problems else 0)


def snapshot(season, folder, stamp):
	earlier = load_snapshots(folder)
	sess = espn_session()

	topics, pages = [], 0
	for page in range(MAX_PAGES):
		if page:
			time.sleep(DELAY_S)
		r = fetch_page(sess, season, page * PAGE_SIZE)
		save(folder, stamp, page, r)
		pages += 1
		if r.status_code == 404 and b"does not exist" in r.content:
			return finish(folder, stamp, 3, f"{season}: feed gone (league rolled over?) - HTTP 404")
		if r.status_code != 200:
			return finish(folder, stamp, 2, f"{season}: HTTP {r.status_code} on page {page}")
		try:
			batch = r.json().get("topics", [])
		except ValueError:
			return finish(folder, stamp, 2, f"{season}: page {page} is not JSON")
		topics.extend(batch)
		if len(batch) < PAGE_SIZE:
			break
	else:
		print(f"WARNING: stopped after {MAX_PAGES} pages; the feed may be longer")

	# Same topic on two pages would mean the feed shifted mid-run
	ids = [t["id"] for t in topics]
	dupes = len(ids) - len(set(ids))
	truncated = [t["id"] for t in topics if len(t.get("messages", [])) < (t.get("totalMessageCount") or 0)]

	now_msgs = {m["id"]: (t, m) for t, m in messages_in(topics)}
	seen_before = {}
	for s, ts in sorted(earlier.items()):
		for t, m in messages_in(ts):
			seen_before.setdefault(m["id"], {"first_seen": s, "topic": t["id"], "type": m.get("messageTypeId"), "date": m.get("date")})
			seen_before[m["id"]]["last_seen"] = s
	new = [i for i in now_msgs if i not in seen_before]
	# Newly gone: in the previous complete snapshot, missing now. Ever gone: seen at any point, missing now.
	previous = {m["id"] for _, m in messages_in(earlier[max(earlier)])} if earlier else set()
	gone = [i for i in previous if i not in now_msgs]
	ever_gone = [i for i in seen_before if i not in now_msgs]

	# index.json: derived from the snapshots, rewritten each run
	index = dict(seen_before)
	for i, (t, m) in now_msgs.items():
		index.setdefault(i, {"first_seen": stamp, "topic": t["id"], "type": m.get("messageTypeId"), "date": m.get("date")})
		index[i]["last_seen"] = stamp
	tmp = os.path.join(folder, "index.json.tmp")
	with open(tmp, "w") as f:
		json.dump(index, f, indent=0, sort_keys=True)
	os.replace(tmp, os.path.join(folder, "index.json"))

	dates = [m.get("date") for _, m in now_msgs.values() if m.get("date")]
	by_type = Counter(m.get("messageTypeId") for _, m in now_msgs.values())
	print(f"season {season} | snapshot {stamp} | {pages} page(s) | {len(topics)} topics")
	print(f"messages: {len(now_msgs)} total | {len(new)} new | {len(gone)} gone since the last snapshot"
	      f" ({len(ever_gone)} missing that any earlier snapshot had)"
	      f" | reach {ms(min(dates) if dates else None)} -> {ms(max(dates) if dates else None)}")
	for mt, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
		print(f"   {mt!s:>5}  {MESSAGE_TYPES.get(mt, 'other'):16} {n}")
	if dupes:
		print(f"WARNING: {dupes} topic(s) appeared on two pages; the feed changed during the run")
	if truncated:
		print(f"WARNING: {len(truncated)} topic(s) returned fewer messages than totalMessageCount: {truncated[:5]}")
	for i in gone[:20]:
		g = seen_before[i]
		print(f"GONE: message {i} (type {g['type']}, {ms(g['date'])}) last seen in {g['last_seen']}")
	status = "warn" if (gone or dupes or truncated) else "ok"
	return finish(folder, stamp, 0, f"{season}: {status} | {len(topics)} topics | {len(now_msgs)} messages | "
	                                f"{len(new)} new | {len(gone)} newly gone | {len(ever_gone)} ever gone | "
	                                f"trades {by_type[244]}/{by_type[224]}/{by_type[230]}")


def log(folder, stamp, line):
	with open(os.path.join(folder, "runs.log"), "a") as f:
		f.write(f"{stamp} {line}\n")


def finish(folder, stamp, code, line):
	log(folder, stamp, f"exit={code} {line}")
	if code:
		print("ERROR:", line)
	return code


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def secret_patterns():
	"""Every non-empty .env value as a case-insensitive pattern; the SWID also matches without braces."""
	pats = {}
	for k, v in dotenv_values(os.path.join(HERE, ".env")).items():
		if v and len(v) >= 8:
			core = v[1:-1] if v.startswith("{") and v.endswith("}") else v
			pats[k] = re.compile(re.escape(core), re.IGNORECASE)
	return pats


def redact(text, pats):
	swid = pats.get("ESPN_SWID")
	return swid.sub(SWID_PLACEHOLDER, text) if swid else text


def git(*args):
	r = subprocess.run([GIT, "-C", ARCHIVE_REPO, *args], capture_output=True, text=True, timeout=300)
	if r.returncode:
		raise RuntimeError(f"git {args[0]} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
	return r.stdout


def backup(season, folder):
	problems = []

	# 1. D: copy - raw files, never deletes anything at the destination
	r = subprocess.run(["robocopy", ARCHIVE_DIR, LOCAL_BACKUP, "/E", "/XJ", "/R:2", "/W:5",
	                    "/NP", "/NFL", "/NDL", "/NJH", "/NJS"], capture_output=True, text=True, timeout=600)
	if r.returncode >= 8:    # robocopy: 0-7 success, 8+ failure
		problems.append(f"robocopy exit {r.returncode}: {r.stdout.strip()[-200:]}")

	# 2. GitHub: redacted copies of snapshot pages not mirrored yet, plus runs.log
	try:
		if not os.path.isdir(os.path.join(ARCHIVE_REPO, ".git")):
			raise RuntimeError(f"archive repo not cloned at {ARCHIVE_REPO}")
		pats = secret_patterns()
		dest = os.path.join(ARCHIVE_REPO, "activity_feed", str(season))
		os.makedirs(dest, exist_ok=True)
		for path in sorted(glob.glob(os.path.join(folder, "*_p*_*.json.gz"))):
			target = os.path.join(dest, os.path.basename(path))
			if os.path.exists(target):
				continue    # snapshots never change once written
			with gzip.open(path, "rb") as f:
				text = redact(f.read().decode("utf-8"), pats)
			with gzip.GzipFile(target, "xb", mtime=0) as f:
				f.write(text.encode("utf-8"))
		with open(os.path.join(folder, "runs.log")) as f:
			log_text = redact(f.read(), pats)
		with open(os.path.join(dest, "runs.log"), "w", newline="\n") as f:
			f.write(log_text)

		git("add", "-A")
		staged = [p for p in git("diff", "--cached", "--name-only").splitlines() if p]
		for rel in staged:
			full = os.path.join(ARCHIVE_REPO, rel)
			if not os.path.exists(full):
				continue    # a deletion; nothing to scan
			with (gzip.open(full) if rel.endswith(".gz") else open(full, "rb")) as f:
				text = f.read().decode("utf-8", "replace")
			leaked = [k for k, p in pats.items() if p.search(text)]
			if leaked:
				git("reset", "-q")
				raise RuntimeError(f"secret scan: {rel} contains {leaked}; nothing committed")
		if staged:
			git("commit", "-q", "-m", f"activity feed {season}: {len(staged)} file(s)")
		git("push", "-q", "origin", "HEAD")    # every run, so a commit from a failed push gets retried
	except Exception as e:
		problems.append(f"github: {e}")
	return problems


if __name__ == "__main__":
	sys.exit(main())
