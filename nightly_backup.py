#!/usr/bin/env python
"""
nightly_backup.py - database backup and housekeeping, run nightly by Task Scheduler
(Rockwood Nightly Backup) and weekly with --push by weekly_maintenance.py.

Steps, each independent (a failure is reported and the next step still runs):
	dump      pg_dump -Fc as rockwood_backup (BACKUP_DATABASE_URL) to DUMP_DIR/rockwood_YYYYMMDD.dump.
	          The dump is written to a temp name and must pass pg_restore --list with every table in
	          REQUIRED_TABLES before it replaces anything. The first dump of each month is also kept
	          in DUMP_DIR/monthly/. Keeps KEEP_DAILY daily and KEEP_MONTHLY monthly copies; other
	          files in DUMP_DIR (e.g. one-off *_preaudit.dump) are never touched.
	espn_raw  robocopy /E of espn_raw/ to RAW_BACKUP: incremental, and never deletes at the
	          destination, so an accidental delete on C: can't propagate to the copy.
	logs      keeps the newest KEEP_API_LOGS rotated logs/api-*.log (NSSM rotates api.log at 10 MB).
	push      --push only: writes the dump as plain SQL to the private rockwood-archive repo as
	          db/rockwood.sql (one file, overwritten) and commits it only when it changed. Plain SQL
	          because git stores each week as a small delta (a binary -Fc dump would add ~1 MB of
	          history every week), and because it can be redacted: the account SWID is a member id in
	          espn_team_map, so it is replaced with activity_feed_archive's SWID_PLACEHOLDER (put the
	          real SWID back before restoring), and the file is scanned for every .env value before
	          commit. Git uses the user's credentials, so weekly_maintenance runs this step, not the
	          nightly task.

Failure alert: NOTIFY_WEBHOOK_URL (the Discord hook weekly_maintenance uses) gets the failed steps.
BACKUP_HEALTHCHECK_URL (optional, a separate daily healthchecks.io check) is pinged on success and
/fail on failure. HEALTHCHECK_URL is weekly_maintenance's weekly check and is never pinged here.

One line per run goes to logs/nightly_backup.log. Exit code: sum of 1 dump, 2 espn_raw, 4 logs, 8 push.
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from activity_feed_archive import ARCHIVE_REPO, git, redact, secret_patterns  # noqa: E402
from weekly_maintenance import redacted, webhook_body  # noqa: E402

PG_BIN = r"C:\Program Files\PostgreSQL\16\bin"
DUMP_DIR = r"D:\Backup\rockwood\db"
MONTHLY_DIR = os.path.join(DUMP_DIR, "monthly")
RAW_DIR = os.path.join(HERE, "espn_raw")
RAW_BACKUP = r"D:\Backup\rockwood\espn_raw"
LOG_DIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOG_DIR, "nightly_backup.log")
ARCHIVE_SQL = "db/rockwood.sql"
KEEP_DAILY = 14
KEEP_MONTHLY = 12
KEEP_API_LOGS = 10
REQUIRED_TABLES = ("box_scores", "draft_picks", "espn_team_map", "matchups", "records", "teams",
                   "tower_scores", "transaction_items", "transactions")
STEP_BITS = {"dump": 1, "espn_raw": 2, "logs": 4, "push": 8}
RUN_LINES = []


def log(step, status, detail):
	stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
	line = f"{stamp} {step:<8} {status:<4} {detail}"
	os.makedirs(LOG_DIR, exist_ok=True)
	with open(LOG, "a", encoding="ascii", errors="replace") as f:
		f.write(line + "\n")
	RUN_LINES.append(line)
	print(line)


def pg(tool, *args, timeout=600):
	"""Run a PostgreSQL client tool as rockwood_backup; the password goes in the environment, not argv."""
	u = urlsplit(os.environ["BACKUP_DATABASE_URL"])
	env = dict(os.environ, PGHOST=u.hostname, PGPORT=str(u.port or 5432), PGUSER=unquote(u.username),
	           PGPASSWORD=unquote(u.password), PGDATABASE=u.path.lstrip("/"))
	r = subprocess.run([os.path.join(PG_BIN, tool), *args], capture_output=True, env=env, timeout=timeout)
	if r.returncode:
		raise RuntimeError(f"{tool} exit {r.returncode}: {r.stderr.decode(errors='replace').strip()[:300]}")
	return r.stdout


def prune(pattern, folder, keep):
	"""Delete all but the newest `keep` files in folder whose names fully match pattern."""
	names = sorted(n for n in os.listdir(folder) if re.fullmatch(pattern, n))
	for n in names[:-keep]:
		os.remove(os.path.join(folder, n))
	return len(names[:-keep])


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
def step_dump(today):
	os.makedirs(MONTHLY_DIR, exist_ok=True)
	final = os.path.join(DUMP_DIR, f"rockwood_{today:%Y%m%d}.dump")
	tmp = final + ".tmp"
	pg("pg_dump.exe", "-Fc", "-f", tmp)
	toc = pg("pg_restore.exe", "--list", tmp).decode(errors="replace")
	missing = [t for t in REQUIRED_TABLES if not re.search(rf"TABLE DATA public {t} ", toc)]
	if missing:
		os.remove(tmp)
		raise RuntimeError(f"dump is missing table data for {missing}; previous dumps kept")
	os.replace(tmp, final)
	monthly = os.path.join(MONTHLY_DIR, f"rockwood_{today:%Y%m}.dump")
	if not os.path.exists(monthly):
		shutil.copy2(final, monthly)
	gone = prune(r"rockwood_\d{8}\.dump", DUMP_DIR, KEEP_DAILY) + prune(r"rockwood_\d{6}\.dump", MONTHLY_DIR, KEEP_MONTHLY)
	log("dump", "ok", f"{os.path.basename(final)} {os.path.getsize(final) / 1e6:.1f} MB, "
	                  f"{len(REQUIRED_TABLES)} tables checked | {gone} old copies removed")
	return final


def step_espn_raw():
	r = subprocess.run(["robocopy", RAW_DIR, RAW_BACKUP, "/E", "/XJ", "/R:2", "/W:5",
	                    "/NP", "/NFL", "/NDL", "/NJH"], capture_output=True, text=True, timeout=3600)
	if r.returncode >= 8:    # robocopy: 0-7 success, 8+ failure
		raise RuntimeError(f"robocopy exit {r.returncode}: {r.stdout.strip()[-200:]}")
	files = re.search(r"Files :\s+(\d+)\s+(\d+)", r.stdout)
	log("espn_raw", "ok", f"robocopy exit {r.returncode}" + (f" | {files.group(2)} of {files.group(1)} files copied" if files else ""))


def step_logs():
	rotated = sorted(glob.glob(os.path.join(LOG_DIR, "api-*.log")), key=os.path.getmtime)
	for path in rotated[:-KEEP_API_LOGS]:
		os.remove(path)
	log("logs", "ok", f"{len(rotated[:-KEEP_API_LOGS])} rotated api logs removed, {min(len(rotated), KEEP_API_LOGS)} kept")


def step_push(dump):
	if not os.path.isdir(os.path.join(ARCHIVE_REPO, ".git")):
		raise RuntimeError(f"archive repo not cloned at {ARCHIVE_REPO}")
	pats = secret_patterns()
	text = redact(pg("pg_restore.exe", "-f", "-", dump).decode("utf-8"), pats)
	leaked = [k for k, p in pats.items() if p.search(text)]
	if leaked:
		raise RuntimeError(f"secret scan: dump contains the value of {leaked}; nothing committed")
	target = os.path.join(ARCHIVE_REPO, ARCHIVE_SQL)
	os.makedirs(os.path.dirname(target), exist_ok=True)
	with open(target, "w", encoding="utf-8", newline="\n") as f:
		f.write(text)
	git("add", ARCHIVE_SQL)
	changed = bool(git("diff", "--cached", "--name-only").strip())
	if changed:
		git("commit", "-q", "-m", f"db dump {os.path.basename(dump)}")
	git("push", "-q", "origin", "HEAD")    # every run, so a commit from a failed push gets retried
	log("push", "ok", f"committed {ARCHIVE_SQL} from {os.path.basename(dump)}" if changed else "data unchanged, nothing committed")


# --------------------------------------------------------------------------- #
# notification
# --------------------------------------------------------------------------- #
def notify(failed):
	text = redacted(f"Rockwood nightly backup: {'ok' if not failed else 'FAILED: ' + ', '.join(failed)}\n"
	                + "\n".join(RUN_LINES))[:1900]
	sends = []
	hc = os.getenv("BACKUP_HEALTHCHECK_URL")
	if hc:
		sends.append(("healthcheck", hc if not failed else hc.rstrip("/") + "/fail", {"data": text.encode("utf-8")}))
	hook = os.getenv("NOTIFY_WEBHOOK_URL")
	if hook and failed:
		sends.append(("webhook", hook, webhook_body(hook, text)))
	for name, url, body in sends:
		try:
			requests.post(url, timeout=15, **body).raise_for_status()
		except requests.RequestException as e:
			# never log the URL or the exception text; the URL is itself a secret
			log("notify", "FAIL", f"{name}: {type(e).__name__}")


def main():
	ap = argparse.ArgumentParser(description="Nightly pg_dump, espn_raw copy and log housekeeping.")
	ap.add_argument("--push", action="store_true", help="also commit the dump to rockwood-archive (weekly)")
	push = ap.parse_args().push

	today = datetime.now().date()
	failed, dump = [], None
	try:
		dump = step_dump(today)
	except Exception as e:
		log("dump", "FAIL", f"{type(e).__name__}: {e}"[:300])
		failed.append("dump")
	for name, fn in (("espn_raw", step_espn_raw), ("logs", step_logs)):
		try:
			fn()
		except Exception as e:    # a crash in one step never stops the next
			log(name, "FAIL", f"{type(e).__name__}: {e}"[:300])
			failed.append(name)
	if push:
		try:
			if not dump:
				raise RuntimeError("no dump from this run to push")
			step_push(dump)
		except Exception as e:
			log("push", "FAIL", f"{type(e).__name__}: {e}"[:300])
			failed.append("push")
	notify(failed)
	return sum(STEP_BITS[s] for s in failed)


if __name__ == "__main__":
	sys.exit(main())
