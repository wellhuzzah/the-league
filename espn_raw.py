"""
espn_raw.py — fetch from ESPN and archive the raw response to disk.

Every response (any status) is saved byte-for-byte, gzipped, under
	espn_raw/{season}/{view[+view...]}/{scoringPeriodId|none}/{UTC timestamp}_{status}.json.gz
before the caller sees it. espn_raw/ is gitignored. Nothing is ever overwritten.

The caller gets the requests.Response back unchanged and does its own
raise_for_status() / .json() handling, so wrapping a fetch in this changes
nothing about how the caller behaves.

Reading the archive: load_archived(path) reads a saved response back. fetch_archived always
makes a live request (then saves it); it is not a cache.
"""

import gzip
import os
from datetime import datetime, timezone

RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "espn_raw")


def archive_path(season, views, period):
	view_part = "+".join(views) if views else "none"
	period_part = str(period) if period is not None else "none"
	return os.path.join(RAW_DIR, str(season), view_part, period_part)


def fetch_archived(sess, url, season, views, period=None, headers=None, timeout=60):
	"""GET url with ?view=... (one per view) and optional scoringPeriodId, archive it, return the Response."""
	params = [("view", v) for v in views]
	if period is not None:
		params.append(("scoringPeriodId", period))
	r = sess.get(url, params=params, headers=headers, timeout=timeout)

	folder = archive_path(season, views, period)
	os.makedirs(folder, exist_ok=True)
	stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
	with gzip.open(os.path.join(folder, f"{stamp}_{r.status_code}.json.gz"), "wb") as f:
		f.write(r.content)
	return r


def load_archived(path):
	"""Read one archived response back as bytes."""
	with gzip.open(path, "rb") as f:
		return f.read()
