#!/usr/bin/env python
"""
backfill_played.py - fill box_scores.played from the archived ESPN box score responses.

played = the player played that week (espn_import.played_game: the actual stat line has a stat beyond
the team result). New imports set it themselves; this fills rows imported before the column existed.
For each season and week it reads the newest archived mBoxscore response (the one the last import
wrote from) and checks every row's points against it before updating; a mismatch is reported and
that row is left NULL.

	venv\\Scripts\\python.exe scripts\\backfill_played.py            # report only
	venv\\Scripts\\python.exe scripts\\backfill_played.py --write    # update rows (as rockwood_writer)
"""

import argparse
import asyncio
import glob
import json
import os
import sys
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
load_dotenv(os.path.join(HERE, ".env"))

from espn_import import actual_points, played_game  # noqa: E402
from espn_raw import RAW_DIR, load_archived  # noqa: E402


def archived_weeks():
	"""(season, week) -> {espn_player_id: (played, actual points or None)} from the newest response."""
	out = {}
	for season_dir in sorted(glob.glob(os.path.join(RAW_DIR, "20*"))):
		season = int(os.path.basename(season_dir))
		for week_dir in glob.glob(os.path.join(season_dir, "mBoxscore+mMatchupScore", "*")):
			week = int(os.path.basename(week_dir))
			files = sorted(glob.glob(os.path.join(week_dir, "*_200.json.gz")))
			if not files:
				continue
			players = {}
			for sched in json.loads(load_archived(files[-1])).get("schedule", []):
				for side in ("home", "away"):
					roster = (sched.get(side) or {}).get("rosterForCurrentScoringPeriod") or {}
					for e in roster.get("entries", []):
						pobj = (e.get("playerPoolEntry") or {}).get("player") or {}
						players[e.get("playerId")] = (played_game(pobj, week), actual_points(pobj, week))
			out[(season, week)] = players
	return out


async def main():
	ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
	ap.add_argument("--write", action="store_true", help="update box_scores (default: report only)")
	write = ap.parse_args().write

	weeks = archived_weeks()
	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	try:
		rows = await conn.fetch("SELECT id, season::int AS season, week, espn_player_id, points_scored, has_stats FROM box_scores")
		updates, missing, mismatched = [], 0, []
		for r in rows:
			found = weeks.get((r["season"], r["week"]), {}).get(r["espn_player_id"])
			if found is None:
				missing += 1
				continue
			played, pts = found
			if pts is not None and Decimal(str(pts)).quantize(Decimal("0.01")) != r["points_scored"]:
				mismatched.append((r["season"], r["week"], r["espn_player_id"], pts, r["points_scored"]))
				continue
			updates.append((played, r["id"]))
		print(f"rows {len(rows)} | from archive {len(updates)} (played {sum(p for p, _ in updates)}, "
		      f"didn't play {sum(not p for p, _ in updates)}) | not in archive {missing} | points mismatch {len(mismatched)}")
		for m in mismatched[:10]:
			print("  mismatch (season, week, player, archive pts, db pts):", m)
		if write:
			async with conn.transaction():
				await conn.executemany("UPDATE box_scores SET played = $1 WHERE id = $2", updates)
			print(f"updated {len(updates)} rows")
	finally:
		await conn.close()


asyncio.run(main())
