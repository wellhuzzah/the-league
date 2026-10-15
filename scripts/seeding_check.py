#!/usr/bin/env python
"""
seeding_check.py - analysis only. Replicates ESPN's playoff seeding as written in
docs/espn_playoff_seeding_faq.md and compares it with the archived playoffSeed values.

Not wired into the API or the frontend. Reads matchups (regular season, week <= 13) from the
database, and divisionId / playoffSeed from archived mTeam responses via load_archived only.

Decisions taken from the user's reading of the FAQ:
	U1  division winners are ordered among themselves by winning percentage
	U3  division winners take the top seeds
	U5  tied teams that never met: H2H separates no one, fall back to Points For
	U7  the process restarts after each seed; all remaining teams are re-ranked by win%
	U10 assumption: byes and reseeding do not affect the regular-season seeds
Still unspecified, so the code stops (Unclear) rather than deciding: U2, U4, U6, U8, U9.
A coin flip is Undecidable. Neither counts as a match or a mismatch.

Each seed k is computed with ESPN's actual seeds 1..k-1 taken as already awarded.

	python scripts/seeding_check.py
"""

import asyncio
import glob
import json
import os
import sys
from collections import defaultdict
from itertools import combinations

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.database import get_pool  # noqa: E402
from espn_raw import RAW_DIR, load_archived  # noqa: E402

REG_SEASON_LAST_WEEK = 13


class Unclear(Exception):
	"""The FAQ does not specify this step."""


class Undecidable(Exception):
	"""The FAQ's last step: a virtual coin flip."""


# --------------------------------------------------------------------------- #
# season data
# --------------------------------------------------------------------------- #
class Season:
	def __init__(self, teams, division, games, names):
		self.teams = teams              # team ids
		self.division = division        # team id -> divisionId
		self.games = games              # (home, away, home_score, away_score, winner or None)
		self.names = names              # team id -> owner
		self.w = defaultdict(int)
		self.l = defaultdict(int)
		self.d = defaultdict(int)
		self.pf = defaultdict(float)
		self.pa = defaultdict(float)
		for h, a, hs, as_, win in games:
			self.pf[h] += hs
			self.pa[h] += as_
			self.pf[a] += as_
			self.pa[a] += hs
			if win is None:
				self.d[h] += 1
				self.d[a] += 1
			else:
				self.w[win] += 1
				self.l[a if win == h else h] += 1

	def win_pct(self, t):
		"""A draw counts as half a win."""
		n = self.w[t] + self.l[t] + self.d[t]
		return (self.w[t] + 0.5 * self.d[t]) / n if n else 0.0

	def meetings(self, a, b):
		return [g for g in self.games if {g[0], g[1]} == {a, b}]

	def n(self, t):
		return self.names.get(t, str(t))

	def ns(self, teams):
		return "[" + ", ".join(self.n(t) for t in teams) + "]"

	def record_line(self, t):
		return f"{self.w[t]}-{self.l[t]}-{self.d[t]} PF {self.pf[t]:.1f} PA {self.pa[t]:.1f}"


# --------------------------------------------------------------------------- #
# FAQ tiebreakers: H2H Record, Points For, IntraDivisional Record, Points Against, Coin Flip
# --------------------------------------------------------------------------- #
def h2h_step(s, tied, trail):
	"""H2H record among the tied teams. Returns the teams with the best H2H record, or the whole
	group if H2H is invalid (the tied teams have not all played each other an equal number of times)."""
	counts = {frozenset(p): len(s.meetings(*p)) for p in combinations(tied, 2)}
	if len(set(counts.values())) != 1:
		trail.append(f"H2H invalid (meetings per pair: {sorted(set(counts.values()))})")
		return tied
	wins = defaultdict(int)
	for a, b in combinations(tied, 2):
		for h, aw, hs, as_, win in s.meetings(a, b):
			if win is None:
				raise Unclear(f"U4: a head-to-head game between tied teams {s.ns(tied)} was a draw; the FAQ does "
				              "not say how a draw counts in H2H record")
			wins[win] += 1
	best = max(wins[t] for t in tied)
	top = [t for t in tied if wins[t] == best]
	trail.append(f"H2H ({counts[frozenset(next(combinations(tied, 2)))]} meeting(s) per pair) wins "
	             + ", ".join(f"{s.n(t)} {wins[t]}" for t in tied)
	             + (f" -> {s.ns(top)}" if len(top) < len(tied) else " -> still tied"))
	return top


def pf_step(s, tied, trail):
	best = max(s.pf[t] for t in tied)
	top = [t for t in tied if s.pf[t] == best]
	trail.append("PF " + ", ".join(f"{s.n(t)} {s.pf[t]:.1f}" for t in tied)
	             + (f" -> {s.ns(top)}" if len(top) < len(tied) else " -> still tied"))
	return top


def intradivision_step(s, tied, trail):
	raise Unclear(f"U9: IntraDivisional Record reached for {s.ns(tied)}; the FAQ does not define how it is "
	              "computed or compared (record against the whole division? only against the other tied teams? "
	              "teams in different divisions?)")


def pa_step(s, tied, trail):
	best = max(s.pa[t] for t in tied)
	top = [t for t in tied if s.pa[t] == best]
	trail.append("PA " + ", ".join(f"{s.n(t)} {s.pa[t]:.1f}" for t in tied)
	             + (f" -> {s.ns(top)}" if len(top) < len(tied) else " -> still tied"))
	return top


def break_tie(s, tied, trail):
	"""Pick the one team that takes the seed from a group with the same winning percentage."""
	top = h2h_step(s, tied, trail)
	if len(top) == 1:
		return top[0], "H2H"
	if len(top) < len(tied):
		raise Unclear(f"U6: {s.ns(top)} share the best H2H record among the tied {s.ns(tied)}; the FAQ does not "
		              "say whether the next tiebreaker applies to just those teams or to the whole tied group")
	top = pf_step(s, tied, trail)
	if len(top) == 1:
		return top[0], "PF"
	if len(tied) > 2:
		raise Unclear(f"U8: Points For is tied among {s.ns(top)} in a multi-team tie {s.ns(tied)}; the FAQ's "
		              "multi-team procedure stops at Points For")
	intradivision_step(s, top, trail)
	top = pa_step(s, top, trail)
	if len(top) == 1:
		return top[0], "PA"
	raise Undecidable(f"coin flip between {s.ns(top)}")


def pick_one(s, candidates, trail):
	"""Best winning percentage among the candidates; tiebreakers if several share it."""
	best = max(s.win_pct(t) for t in candidates)
	tied = [t for t in candidates if s.win_pct(t) == best]
	if len(tied) == 1:
		trail.append(f"win% {best:.3f}")
		return tied[0], "win%"
	trail.append(f"win% {best:.3f} tied: {s.ns(tied)}")
	return break_tie(s, tied, trail)


def division_winner(s, teams, d):
	"""A division's winner: best winning percentage; a tie is settled by H2H as in the FAQ's example."""
	trail = [f"division {d} winner"]
	t, step = pick_one(s, teams, trail)
	if step not in ("win%", "H2H"):
		raise Unclear(f"U2: division {d} tie not settled by H2H (reached {step}); the FAQ's example only shows "
		              "H2H deciding a division")
	return t, step, trail


def seed_one(s, k, earlier):
	"""Seed k, taking ESPN's seeds 1..k-1 (earlier) as already awarded.
	Division winners take the top seeds, ordered by winning percentage (U1, U3); then everyone else by
	winning percentage; the process restarts for every seed (U7).
	Returns (team, deciding step, trail); raises Unclear / Undecidable."""
	divisions = defaultdict(list)
	for t in s.teams:
		divisions[s.division[t]].append(t)
	if k <= len(divisions):
		# Division-winner seeds: winners of the divisions with no team among the earlier seeds.
		open_divs = [d for d in sorted(divisions) if not any(s.division[t] == d for t in earlier)]
		winners, notes = [], []
		for d in open_divs:
			t, step, trail = division_winner(s, divisions[d], d)
			winners.append(t)
			notes += trail + [f"-> {s.n(t)} ({step})"]
		if len(winners) == 1:
			return winners[0], "division winner", notes
		trail = notes + ["among division winners"]
		t, step = pick_one(s, winners, trail)
		return t, f"division winners: {step}", trail
	trail = []
	t, step = pick_one(s, [t for t in s.teams if t not in earlier], trail)
	return t, step, trail


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
def archived_teams(season):
	f = sorted(glob.glob(os.path.join(RAW_DIR, str(season), "mTeam", "*", "*_200.json.gz")))[-1]
	return json.loads(load_archived(f))["teams"]


def archived_playoff_team_count(season):
	"""Teams that appear in the archived schedule's WINNERS_BRACKET matchups."""
	teams = set()
	for f in glob.glob(os.path.join(RAW_DIR, str(season), "mBoxscore+mMatchupScore", "*", "*_200.json.gz")):
		for m in json.loads(load_archived(f)).get("schedule", []):
			if m.get("playoffTierType") == "WINNERS_BRACKET":
				for side in ("home", "away"):
					if m.get(side):
						teams.add(m[side]["teamId"])
	return len(teams)


async def load_all(seasons):
	pool = await get_pool()
	out = {}
	async with pool.acquire() as db:
		owner = {r["team_id"]: r["owner"] for r in await db.fetch("SELECT team_id, owner FROM teams")}
		for season in seasons:
			tmap = {r["espn_team_id"]: r["team_id"] for r in await db.fetch(
				"SELECT espn_team_id, team_id FROM espn_team_map WHERE season = $1", season)}
			games = [(r["home_team_id"], r["away_team_id"], float(r["home_score"]), float(r["away_score"]), r["winner_team_id"])
			         for r in await db.fetch("""
				SELECT home_team_id, away_team_id, home_score, away_score, winner_team_id
				FROM matchups WHERE season = $1 AND week <= $2
				  AND home_score IS NOT NULL AND away_score IS NOT NULL""", season, REG_SEASON_LAST_WEEK)]
			out[season] = (tmap, owner, games)
	return out


def main():
	print("U10 (assumption): byes and reseeding are taken not to affect the regular-season seeds.")
	print("The seeding rule is archived for 2026 only (playoffSeedingRule H2H_RECORD); the 2018-2025 rule is not archived.")
	stops = []          # (season, seed, kind, message)
	totals = []
	data = asyncio.run(load_all(range(2018, 2026)))
	for season in range(2018, 2026):
		tmap, owner, games = data[season]
		teams = archived_teams(season)
		division = {tmap[t["id"]]: t["divisionId"] for t in teams}
		espn_seed = {tmap[t["id"]]: t["playoffSeed"] for t in teams}
		espn_by_seed = {v: k for k, v in espn_seed.items()}
		n = archived_playoff_team_count(season)
		s = Season(sorted(division), division, games, owner)
		print(f"\n===== {season}: {n} seeds awarded (archived WINNERS_BRACKET)")
		results = []
		for k in range(1, n + 1):
			earlier = [espn_by_seed[j] for j in range(1, k)]
			e = espn_by_seed[k]
			trail, msg = [], ""
			try:
				t, step, trail = seed_one(s, k, earlier)
				outcome = "match" if t == e else "MISMATCH"
			except Unclear as x:
				t, step, outcome, msg = None, None, "UNCLEAR", str(x)
				stops.append((season, k, "Unclear", msg))
			except Undecidable as x:
				t, step, outcome, msg = None, None, "UNDECIDABLE", str(x)
				stops.append((season, k, "Undecidable", msg))
			results.append(outcome)
			mine = f"{s.n(t):8} ({s.record_line(t)}, div {division[t]})" if t else "-"
			print(f"   #{k} {outcome:11} mine {mine} | ESPN {s.n(e):8} ({s.record_line(e)}, div {division[e]}) | decided by {step}")
			if outcome != "match" or (step and step != "win%"):
				print("            " + (msg if msg else " ; ".join(trail)))
		strict = 0
		for r in results:
			if r != "match":
				break
			strict += 1
		decided = [r for r in results if r in ("match", "MISMATCH")]
		cond = sum(1 for r in decided if r == "match")
		totals.append((season, n, strict, cond, len(decided), results.count("UNCLEAR"), results.count("UNDECIDABLE")))
	print("\n===== summary")
	print("strict = seeds matched in a row from #1, stopping at the first seed that is not a match")
	print("conditional = seeds matched given ESPN's earlier seeds, out of the seeds that reached a decision")
	for season, n, strict, cond, decided, unc, und in totals:
		print(f"   {season}: strict {strict}/{n} | conditional {cond}/{decided} | Unclear {unc} | Undecidable {und}")
	print("\n===== every stop")
	for season, k, kind, msg in stops:
		print(f"   {season} seed #{k}: {kind}: {msg}")


if __name__ == "__main__":
	main()
