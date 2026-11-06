#!/usr/bin/env python
"""
backfill_playoff_tier.py - fill matchups.playoff_tier from the archived ESPN schedules.

playoff_tier = the schedule entry's playoffTierType: NONE (regular season), WINNERS_BRACKET,
WINNERS_CONSOLATION_LADDER or LOSERS_CONSOLATION_LADDER. New imports set it themselves; this fills
rows imported before the column existed. For each season it reads the newest archived
mMatchup+mMatchupScore response (the whole schedule) and matches each decided game to its matchups
row by season, week and teams, checking both scores first; a mismatch or a row with no archived
game is reported and left NULL. Byes (an entry with no away team) have no matchups row.

	venv\\Scripts\\python.exe scripts\\backfill_playoff_tier.py            # report only
	venv\\Scripts\\python.exe scripts\\backfill_playoff_tier.py --write    # update rows (as rockwood_writer)
"""

import argparse
import asyncio
import glob
import json
import os
import sys
from collections import Counter
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
load_dotenv(os.path.join(HERE, ".env"))

from espn_raw import RAW_DIR, load_archived  # noqa: E402


def archived_schedules():
	"""season -> schedule entries from the newest mMatchup+mMatchupScore response."""
	out = {}
	for season_dir in sorted(glob.glob(os.path.join(RAW_DIR, "20*"))):
		files = sorted(glob.glob(os.path.join(season_dir, "mMatchup+mMatchupScore", "none", "*_200.json.gz")))
		if files:
			data = json.loads(load_archived(files[-1]))
			out[int(os.path.basename(season_dir))] = (data[0] if isinstance(data, list) else data).get("schedule", [])
	return out


def points(x):
	return Decimal(str(x)).quantize(Decimal("0.01"))


async def main():
	ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
	ap.add_argument("--write", action="store_true", help="update matchups (default: report only)")
	write = ap.parse_args().write

	conn = await asyncpg.connect(os.getenv("DATABASE_URL"), ssl=False)
	try:
		tmap = {(int(r["season"]), r["espn_team_id"]): r["team_id"]
		        for r in await conn.fetch("SELECT season, espn_team_id, team_id FROM espn_team_map")}
		rows = {(int(r["season"]), r["week"], r["home_team_id"], r["away_team_id"]): r
		        for r in await conn.fetch("SELECT id, season, week, home_team_id, away_team_id, home_score, "
		                                  "away_score, playoff_tier FROM matchups")}
		updates, mismatched, tiers = [], [], Counter()
		for season, schedule in archived_schedules().items():
			for e in schedule:
				if not e.get("away") or e.get("winner") == "UNDECIDED":
					continue    # a bye, or not played yet: no matchups row
				key = (season, e["matchupPeriodId"], tmap.get((season, e["home"]["teamId"])),
				       tmap.get((season, e["away"]["teamId"])))
				row = rows.pop(key, None)
				if row is None:
					mismatched.append(("no matchups row", key))
				elif (points(e["home"]["totalPoints"]), points(e["away"]["totalPoints"])) != (row["home_score"], row["away_score"]):
					mismatched.append(("scores differ", key))
				else:
					tier = e.get("playoffTierType")
					tiers[tier] += 1
					updates.append((tier, row["id"]))
		print(f"matched {len(updates)} | tiers {dict(tiers)} | mismatches {len(mismatched)} | "
		      f"matchups rows with no archived game {len(rows)}")
		for m in mismatched[:10]:
			print("  mismatch:", m)
		for key in list(rows)[:10]:
			print("  no archived game for (season, week, home, away):", key)
		if write:
			async with conn.transaction():
				await conn.executemany("UPDATE matchups SET playoff_tier = $1 WHERE id = $2", updates)
			print(f"updated {len(updates)} rows")
	finally:
		await conn.close()


asyncio.run(main())
