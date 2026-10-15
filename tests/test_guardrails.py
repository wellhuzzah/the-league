r"""
Offline replay of weekly_maintenance's guardrails on captured dry runs: no database, ESPN, webhook or
healthcheck. The checks are pure functions; run() / query() / requests.post are replaced with stubs
that fail the test if anything calls them, and the log and maintenance_output/ are compared before
and after.

	venv\Scripts\python.exe -B -m unittest discover -s tests -v

Fixtures (tests/fixtures/) are copies of maintenance_output/ captures, plus a feed extract that keeps
only the trade topics' numeric fields (the raw feed carries account ids, so it is never copied).
"""

import gzip
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import weekly_maintenance as wm

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def captured(name):
	"""(exit code, stdout + stderr) as run() returned them. A capture file is
	'$ args\\nexit=N\\n\\n' + stdout + '\\n--- stderr ---\\n' + stderr; reading it with universal
	newlines gives the same '\\n' text that subprocess's text mode gave the wrapper."""
	with open(os.path.join(FIX, name), encoding="utf-8") as f:
		text = f.read()
	head, body = text.split("\n\n", 1)
	stdout, stderr = body.rsplit("\n--- stderr ---\n", 1)
	return int(head.split("exit=", 1)[1]), stdout + stderr


def swap(text, old, new, count=1):
	"""text with old replaced by new; fails the test if the fixture doesn't contain old exactly count times."""
	n = text.count(old)
	if n != count:
		raise AssertionError(f"fixture anchor found {n} times, expected {count}: {old!r}")
	return text.replace(old, new)


SLOT = "box wk5 team 6: 8 starters (expected 9) \u2014 empty lineup slot(s)."
SUM = "box wk5 team 6: starter sum 120.60 != ESPN 128.6."
UNKNOWN = "matchup wk5: unknown espn team id(s) 15/6; skipped."
BLOCK = f"=== WARNINGS (1) ===\n  - {SLOT}\n\n"

# State before each captured run (docs/postmortem_2026-10-15.md; weekly_maintenance.log)
STATUS_1009 = {"latestScoringPeriod": 5, "finalScoringPeriod": 17}
STATUS_1015 = {"latestScoringPeriod": 6, "finalScoringPeriod": 17}
ROWS = {1: 215, 2: 218, 3: 220, 4: 222}
STORED_1009_2045 = {w: {"matchups": 7, "box_rows": ROWS[w]} for w in (1, 2, 3)}     # before "weeks 1-4 (+1)"
STORED_1015 = {w: {"matchups": 7, "box_rows": ROWS[w]} for w in (1, 2, 3, 4)}       # also before 10-09 21:17
TEAMS, DRAFTED = 14, 210

TRADE_DATE = 1791914625609                       # 2026-10-13 18:03:45Z, the 290 message
TRADE = {"m": (8, 4, 10, TRADE_DATE)}            # ESPN team 8 sent $10 to ESPN team 4
TEAM2 = "    ESPN team 2 (team_id 2): spent 9; WAIVER 8 X; WAIVER+FA 8 X\n"


# --------------------------------------------------------------------------- #
# isolation: nothing in these tests may touch the DB, ESPN, a webhook or the run's files
# --------------------------------------------------------------------------- #
def _state():
	files = [wm.LOG, wm.LAST_SUCCESS]
	if os.path.isdir(wm.OUTPUT_DIR):
		files += sorted(os.path.join(wm.OUTPUT_DIR, n) for n in os.listdir(wm.OUTPUT_DIR))
	return {p: (os.path.getsize(p), os.path.getmtime(p)) if os.path.exists(p) else None for p in files}


def _forbidden(name):
	def stub(*a, **k):
		raise AssertionError(f"{name} called during offline tests")
	return stub


_before, _patches = None, []


def setUpModule():
	global _before
	_before = _state()
	for target in ("run", "query"):
		_patches.append(mock.patch.object(wm, target, _forbidden(f"weekly_maintenance.{target}")))
	_patches.append(mock.patch.object(wm.requests, "post", _forbidden("requests.post")))
	for p in _patches:
		p.start()


def tearDownModule():
	for p in reversed(_patches):
		p.stop()
	if _state() != _before:
		raise AssertionError("the tests changed weekly_maintenance.log, last_success or maintenance_output/")


# --------------------------------------------------------------------------- #
# scores
# --------------------------------------------------------------------------- #
class Scores(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		code, cls.out = captured("20261015T120016Z_scores_dry.txt")
		assert code == 0 and cls.out.count(BLOCK) == 1, "10-15 scores fixture changed"

	def check(self, out, stored=STORED_1015, status=STATUS_1015):
		return wm.check_scores(out, TEAMS, stored, DRAFTED, status)

	def warnings(self, *items):
		return swap(self.out, BLOCK, f"=== WARNINGS ({len(items)}) ===\n" + "".join(f"  - {w}\n" for w in items) + "\n")

	def test_1015_passes_with_the_empty_slot_tolerated(self):
		problems, info = self.check(self.out)
		self.assertEqual(problems, [])
		self.assertEqual(info["slots_new"], [(5, 6, 8)])
		self.assertEqual(info["slots_stored"], 0)
		self.assertEqual((info["new_max"], info["db_max"], info["rows_seen"]), (5, 4, 1097))

	def test_slot_in_an_already_stored_week_is_counted_not_new(self):
		stored = {**STORED_1015, 5: {"matchups": 7, "box_rows": 222}}
		problems, info = self.check(self.out, stored)
		self.assertEqual(problems, [])
		self.assertEqual((info["slots_new"], info["slots_stored"]), ([], 1))

	def test_1009_runs_still_pass(self):
		for name, stored in (("20261009T204511Z_scores_dry.txt", STORED_1009_2045),
		                     ("20261009T211732Z_scores_dry.txt", STORED_1015)):
			code, out = captured(name)
			self.assertIn("\nNo warnings.\n", out)
			problems, info = self.check(out, stored, STATUS_1009)
			self.assertEqual((code, problems, info["slots_new"]), (0, [], []), name)

	def test_starter_sum_warning_is_fatal_and_unexcuses_the_slot(self):
		problems, _ = self.check(self.warnings(SLOT, SUM))
		self.assertEqual(problems, [f"dry run warnings: {SLOT}; {SUM}"])

	def test_starter_sum_warning_elsewhere_leaves_the_slot_tolerated(self):
		other = SUM.replace("team 6", "team 9")
		problems, info = self.check(self.warnings(SLOT, other))
		self.assertEqual(problems, [f"dry run warnings: {other}"])
		self.assertEqual(info["slots_new"], [(5, 6, 8)])

	def test_unknown_team_warning_is_fatal(self):
		problems, _ = self.check(self.warnings(SLOT, UNKNOWN))
		self.assertEqual(problems, [f"dry run warnings: {UNKNOWN}"])

	def test_more_than_nine_starters_is_fatal(self):
		ten = SLOT.replace(": 8 starters", ": 10 starters")
		problems, _ = self.check(self.warnings(ten))
		self.assertEqual(problems, [f"dry run warnings: {ten}"])

	def test_stray_line_in_the_warnings_block_is_unreadable(self):
		for out in (swap(self.out, BLOCK, BLOCK[:-1] + "  stray text\n\n"),         # not a "  - " item
		            swap(self.out, BLOCK, BLOCK[:-1] + f"  - {UNKNOWN}\n\n"),      # header still says (1)
		            swap(self.out, BLOCK, BLOCK.replace("(1)", "(2)"))):           # header says 2, one item
			with self.assertRaisesRegex(wm.Unreadable, "^could not read the dry run warnings$"):
				self.check(out)

	def test_unexplained_starter_shortfall_still_trips_the_week_check(self):
		out = swap(self.out, '"starters": 125', '"starters": 121')
		problems, _ = self.check(out)
		self.assertEqual(problems, ["wk5: 121 starters (1 empty slots explained), expected about 126"])

	def test_shortfall_explained_by_tolerated_slots_passes(self):
		out = swap(self.warnings(SLOT, SLOT.replace("team 6: 8", "team 3: 5")), '"starters": 125', '"starters": 121')
		problems, info = self.check(out)
		self.assertEqual(problems, [])
		self.assertEqual(info["slots_new"], [(5, 6, 8), (5, 3, 5)])

	def test_any_count_below_nine_is_tolerated(self):
		# 2025 playoffs had three 1-starter weeks; 0 (a fully empty lineup) is let through the same way
		for k in (0, 1, 7, 8):
			w = SLOT.replace("wk5", "wk15").replace(": 8 starters", f": {k} starters")
			self.assertEqual(wm.split_warnings([w]), ([(15, 6, k)], []))
		self.assertEqual(wm.split_warnings([SLOT.replace("\u2014", "-")])[1], [SLOT.replace("\u2014", "-")])


# --------------------------------------------------------------------------- #
# waivers
# --------------------------------------------------------------------------- #
class Waivers(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		code, cls.out = captured("20261015T120021Z_waivers_dry.txt")
		assert code == 0 and "WAIVER only 12/14" in cls.out, "10-15 waivers fixture changed"
		swap(cls.out, "\n[truncation]", "\n[truncation]")

	def check(self, out=None, feed=None, manual=(), db_rows=142, db_week=5):
		return wm.check_waivers(self.out if out is None else out, db_rows, db_week, feed, list(manual))

	def test_1015_passes_with_the_feed_trade(self):
		with tempfile.TemporaryDirectory() as d:
			write_feed(d, "20261015T120001067768Z", load_feed_fixture())
			feed = wm.feed_faab_trades(2026, d)
		self.assertEqual(list(feed.values()), [(8, 4, 10, TRADE_DATE)])
		problems, info = self.check(feed=feed)
		self.assertEqual(problems, [])
		self.assertEqual((info["kept"], info["latest"], info["trades"]), (190, 6, feed))

	def test_mismatch_with_no_feed_trade_fails_naming_the_pair(self):
		problems, _ = self.check(feed={})
		self.assertEqual(problems, [
			"FAAB spent matches ESPN for only 12/14 teams after trades (ESPN minus expected: team 4 -10, team 8 +10); "
			"looks like $10 from ESPN team 8 to ESPN team 4 moved in a trade that neither the feed nor "
			"faab_adjustments.json shows"])

	def test_unavailable_feed_fails_and_says_so(self):
		problems, _ = self.check(feed=None)
		self.assertEqual(len(problems), 1)
		self.assertTrue(problems[0].endswith("faab_adjustments.json shows; no archived feed snapshot could be read"))

	def test_manual_adjustment_is_the_fallback(self):
		self.assertEqual(self.check(feed=None, manual=[(8, 4, 10, "2026-10-13 trade")])[0], [])

	def test_feed_and_manual_for_the_same_trade_double_count(self):
		problems, _ = self.check(feed=TRADE, manual=[(8, 4, 10, "dup")])
		self.assertIn("(ESPN minus expected: team 4 +10, team 8 -10); looks like $10 from ESPN team 4 to ESPN team 8", problems[0])

	def test_feed_trade_with_the_wrong_amount_fails(self):
		problems, _ = self.check(feed={"m": (8, 4, 5, TRADE_DATE)})
		self.assertIn("(ESPN minus expected: team 4 -5, team 8 +5); looks like $5 from ESPN team 8 to ESPN team 4", problems[0])

	def test_feed_trade_in_the_wrong_direction_fails(self):
		problems, _ = self.check(feed={"m": (4, 8, 10, TRADE_DATE)})
		self.assertIn("(ESPN minus expected: team 4 -20, team 8 +20); looks like $20 from ESPN team 8 to ESPN team 4", problems[0])

	def test_feed_trade_between_teams_that_already_match_fails(self):
		problems, _ = self.check(feed=dict(TRADE, x=(2, 3, 5, TRADE_DATE)))
		self.assertIn("only 12/14 teams after trades (ESPN minus expected: team 2 -5, team 3 +5)", problems[0])

	def test_three_team_mismatch_fails(self):
		out = swap(swap(self.out, "WAIVER only 12/14", "WAIVER only 11/14"), "\n[truncation]", "\n" + TEAM2 + "[truncation]")
		problems, _ = self.check(out, feed=TRADE)
		self.assertEqual(problems, ["FAAB spent matches ESPN for only 13/14 teams after trades (ESPN minus expected: team 2 +1)"])
		problems, _ = self.check(out, feed={})
		self.assertIn("only 11/14 teams after trades (ESPN minus expected: team 2 +1, team 4 -10, team 8 +10); "
		              "looks like $10 from ESPN team 8 to ESPN team 4", problems[0])

	def test_two_teams_that_dont_net_to_zero_fail_without_a_pair(self):
		out = swap(self.out, "spent 10; WAIVER 0 X", "spent 12; WAIVER 0 X")
		problems, _ = self.check(out, feed=TRADE)
		self.assertEqual(problems, ["FAAB spent matches ESPN for only 13/14 teams after trades (ESPN minus expected: team 8 +2)"])

	def test_missing_spent_figure_fails(self):
		out = swap(self.out, "spent 10; WAIVER 0 X", "spent None; WAIVER 0 X")
		problems, _ = self.check(out, feed=TRADE)
		self.assertEqual(problems, ["ESPN team 8 has no FAAB spent figure (None)"])

	def test_header_disagreeing_with_team_lines_is_unreadable(self):
		with self.assertRaisesRegex(wm.Unreadable, "^could not read the dry run checks$"):
			self.check(swap(self.out, "WAIVER only 12/14", "WAIVER only 13/14"), feed=TRADE)

	def test_1009_runs_still_pass(self):
		# the 10-13 trade wasn't in ESPN or the feed yet; the feed at the time had no 290 message
		for name, db_rows in (("20261009T204522Z_waivers_dry.txt", 139), ("20261009T211742Z_waivers_dry.txt", 142)):
			code, out = captured(name)
			self.assertIn("WAIVER only 14/14", out)
			self.assertEqual((code, self.check(out, feed={}, db_rows=db_rows)[0]), (0, []), name)

	def test_feed_trade_that_espn_does_not_show_yet_fails(self):
		_, out = captured("20261009T211742Z_waivers_dry.txt")
		problems, _ = self.check(out, feed=TRADE)
		self.assertIn("(ESPN minus expected: team 4 +10, team 8 -10)", problems[0])


# --------------------------------------------------------------------------- #
# reading the feed archive and faab_adjustments.json
# --------------------------------------------------------------------------- #
def load_feed_fixture():
	with open(os.path.join(FIX, "20261015_feed_trades.json"), encoding="utf-8") as f:
		return json.load(f)["topics"]


def write_feed(root, stamp, topics, status=200, page=0):
	folder = os.path.join(root, "2026")
	os.makedirs(folder, exist_ok=True)
	with gzip.open(os.path.join(folder, f"{stamp}_p{page:03d}_{status}.json.gz"), "wb") as f:
		f.write(json.dumps({"topics": topics}).encode("utf-8"))


class Feed(unittest.TestCase):
	def setUp(self):
		self.dir = tempfile.mkdtemp()
		self.topics = load_feed_fixture()
		self.assertEqual(sorted(m["messageTypeId"] for t in self.topics for m in t["messages"] if m["messageTypeId"] > 280),
		                 [284, 290], "feed fixture changed")

	def tearDown(self):
		shutil.rmtree(self.dir)

	def test_no_snapshot_is_unavailable(self):
		self.assertIsNone(wm.feed_faab_trades(2026, self.dir))

	def test_accepted_but_not_completed_trade_is_ignored(self):
		write_feed(self.dir, "20261013T180000000000Z", [t for t in self.topics if t["id"] != "topic-8"])
		self.assertEqual(wm.feed_faab_trades(2026, self.dir), {})

	def test_incomplete_run_is_skipped(self):
		write_feed(self.dir, "20261015T120001067768Z", self.topics)
		write_feed(self.dir, "20261015T120001067768Z", [], status=500, page=1)
		self.assertIsNone(wm.feed_faab_trades(2026, self.dir))

	def test_corrupt_page_is_skipped(self):
		os.makedirs(os.path.join(self.dir, "2026"))
		with open(os.path.join(self.dir, "2026", "20261015T120001067768Z_p000_200.json.gz"), "wb") as f:
			f.write(b"not gzip")
		self.assertIsNone(wm.feed_faab_trades(2026, self.dir))

	def test_trade_in_an_older_snapshot_survives_a_newer_one_without_it(self):
		write_feed(self.dir, "20261015T120001067768Z", self.topics)
		write_feed(self.dir, "20261022T120000000000Z", [])
		self.assertEqual(list(wm.feed_faab_trades(2026, self.dir).values()), [(8, 4, 10, TRADE_DATE)])

	def test_other_season_is_not_read(self):
		write_feed(self.dir, "20261015T120001067768Z", self.topics)
		self.assertIsNone(wm.feed_faab_trades(2025, self.dir))


class Adjustments(unittest.TestCase):
	def entries(self, *entries):
		d = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, d)
		path = os.path.join(d, "faab_adjustments.json")
		with open(path, "w", encoding="utf-8") as f:
			json.dump(list(entries), f)
		return path

	def test_absent_file_means_none(self):
		self.assertEqual(wm.faab_adjustments(2026, os.path.join(tempfile.gettempdir(), "no-such-dir", "x.json")), [])

	def test_entries_for_the_season(self):
		path = self.entries({"season": 2026, "from_espn_team": 8, "to_espn_team": 4, "amount": 10, "note": "trade"},
		                    {"season": 2025, "from_espn_team": 1, "to_espn_team": 2, "amount": 5, "note": "old"})
		self.assertEqual(wm.faab_adjustments(2026, path), [(8, 4, 10, "trade")])

	def test_malformed_entries_are_unreadable(self):
		good = {"season": 2026, "from_espn_team": 8, "to_espn_team": 4, "amount": 10, "note": "trade"}
		for bad in (dict(good, to_espn_team=8), dict(good, amount=0), dict(good, amount="10"), dict(good, note=" ")):
			with self.assertRaisesRegex(wm.Unreadable, "^faab_adjustments.json entry 0 is malformed$"):
				wm.faab_adjustments(2026, self.entries(bad))


# --------------------------------------------------------------------------- #
# heads-up post and "new since the last ok run"
# --------------------------------------------------------------------------- #
class HeadsUp(unittest.TestCase):
	def setUp(self):
		self.sent = []
		env = {"NOTIFY_WEBHOOK_URL": "https://discord.com/api/webhooks/0/test", "HEALTHCHECK_URL": ""}
		for p in (mock.patch.dict(os.environ, env), mock.patch.object(wm, "RUN_LINES", ["line"]),
		          mock.patch.object(wm, "HEADS_UP", []), mock.patch.object(wm, "redacted", lambda t: t),
		          mock.patch.object(wm.requests, "post", lambda url, **k: self.sent.append(k["json"]) or mock.Mock())):
			p.start()
			self.addCleanup(p.stop)

	def test_clean_run_with_a_new_event_posts_one_line(self):
		wm.HEADS_UP.append("empty lineup slot wk5 team 6 (8 starters)")
		wm.notify([], None)
		self.assertEqual(self.sent, [{"content": "Rockwood weekly maintenance: ok, heads-up: empty lineup slot wk5 team 6 (8 starters)"}])

	def test_clean_run_without_events_posts_nothing(self):
		wm.notify([], None)
		self.assertEqual(self.sent, [])

	def test_failed_run_posts_the_usual_failure_not_the_heads_up(self):
		wm.HEADS_UP.append("empty lineup slot wk5 team 6 (8 starters)")
		wm.notify(["waivers"], None)
		self.assertEqual(self.sent, [{"content": "Rockwood weekly maintenance: FAILED: waivers\nline"}])

	def test_last_ok_reads_the_newest_ok_line_for_the_step(self):
		d = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, d)
		path = os.path.join(d, "weekly_maintenance.log")
		with open(path, "w", encoding="ascii") as f:
			f.write("2026-10-09T21:17:46Z waivers ok    periods 0-5 | transactions 142 (+0)\n"
			        "2026-10-15T12:00:21Z waivers FAIL  guardrail: ...\n"
			        "2026-10-15T12:00:16Z scores  ok    weeks 1-5\n")
		self.assertEqual(wm.last_ok("waivers", path), datetime(2026, 10, 9, 21, 17, 46, tzinfo=timezone.utc))
		self.assertIsNone(wm.last_ok("feed", path))
		self.assertGreater(TRADE_DATE, wm.last_ok("waivers", path).timestamp() * 1000)    # the 10-13 trade is new


if __name__ == "__main__":
	unittest.main()
