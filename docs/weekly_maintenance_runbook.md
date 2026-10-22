# Weekly maintenance runbook

`weekly_maintenance.py` runs every Thursday at 08:00 (Task Scheduler: *Rockwood Weekly Maintenance*).
Each run writes one line per step to `weekly_maintenance.log`; the full output of every dry and
live run is in `maintenance_output/<time>_<step>_<dry|live|run>.txt`. Start there.

A tripped guardrail writes **nothing** for that step. The next Thursday re-checks from scratch,
so a one-off ESPN hiccup usually clears itself. If it doesn't, investigate with the rows below.

## Re-running by hand

From a **PowerShell or cmd window** (not Git Bash, and not Claude Code's `!`, which have no real console):

```
cd C:\projects\rockwood
venv\Scripts\python.exe weekly_maintenance.py                        # normal run
venv\Scripts\python.exe espn_import.py --season 2026 --dry-run       # look without writing
venv\Scripts\python.exe weekly_maintenance.py --accept-diffs scores  # override, see below
```

Only use `--accept-diffs <step>` once you know why the guardrail tripped and that ESPN is right.
It shows the problems and needs `ACCEPT` typed; the override is logged as a `warn` line. It is
refused without a console, so the scheduled task can never use it. An unreadable dry run or an
ESPN error can't be overridden.

## Feed step (`activity_feed_archive.py`)

| Log line | Meaning | Check |
|---|---|---|
| `exit=2 ESPN error` | HTTP error or bad JSON from the activity feed | Cookies in `.env` (ESPN_S2 expires); ESPN outage. Retry later. |
| `exit=3 feed gone DURING the season` | ESPN no longer has this season's feed | Has the commissioner renewed the league early? Older snapshots are safe in `espn_raw\activity_feed`, on D: and on GitHub. |
| `info feed gone after the season` | Normal after rollover | Nothing. Disable the feed step or leave it; it just logs this line. |
| `exit=4 snapshot ok, backup failed` | Snapshot saved; D: copy or GitHub push failed | `espn_raw\activity_feed\<season>\runs.log` names the part. D: missing or full; Git auth (see the scheduled-task notes); `secret scan` = a .env value in a file about to be pushed: find it, don't push. The next run retries the push. |
| `GONE: message …` (in the output) | A message present last time is missing now | ESPN deleted or edited it. The earlier snapshot still has it. |

## Scores step (`espn_import.py`)

| Guardrail | Meaning | Check |
|---|---|---|
| dry run exit / ESPN or import error | ESPN error, cookies, or the importer's own stop (unknown slot, empty team map) | Last lines of the `scores_dry` output. |
| could not read the dry run summary | Importer output changed shape | Was `espn_import.py` edited? The wrapper's parser must match. |
| could not read the dry run warnings | The WARNINGS block isn't the header's count of `  - ` lines | Was `espn_import.py` edited? Can't be overridden. |
| dry run warnings: … | Any importer warning except a tolerated empty slot (below), e.g. a matchup with an unknown team id, a starter sum that differs from ESPN, more than 9 starters | The WARNINGS section of the output; `espn_team_map` for the season. |
| team map N teams, expected M | Team count changed | A team added or removed in ESPN? `espn_team_map` rows. |
| completed weeks not contiguous | A gap in decided weeks | ESPN schedule for that week; a postponed game? |
| fewer completed weeks than stored | ESPN reports a decided week as undecided again | ESPN re-scoring after a correction; wait a day and dry-run again. |
| N new weeks at once | More than 2 weeks since the last import | The task missed runs (check Task Scheduler history). Override if the weeks look right. |
| week marked complete but current period is N | Importing a week still being played | ESPN `status`; don't override during a live week. |
| wkN: M matchups, expected 7 | Regular-season week with the wrong number of games | ESPN schedule; a team missing from the map. |
| wkN: starters (M empty slots explained), expected about 126 | More than 3 starters missing that no empty-slot warning accounts for | ESPN returning partial rosters. Compare with ESPN's site. |
| wkN: rows vs stored median | A new week's roster count is far from normal | Partial ESPN response; roster size change mid-season. |
| wkN (playoffs): matchups | Playoff week outside 1–7 games | Playoff settings changed? |
| stored rows missing from ESPN | ESPN no longer returns players we stored | Player id changes or a partial response. Don't override without comparing with ESPN's site. |
| lineup changed after the fact | Stored starters now benched, or new starters in a scored week | Late ESPN lineup corrections. Verify on ESPN, then override. |
| point changes in older weeks / more than 40 | Stat corrections beyond the latest week | NFL stat corrections can land late; check a couple of players on ESPN, then override. |
| draft: ESPN N picks, stored M | Draft data changed | Keeper or pick edits by the commissioner. |
| post-check: … differ | Database doesn't match the dry run after writing | Look at the `scores_live` output; re-run the dry run to compare. |

**Tolerated, not a trip:** `box wkN team T: K starters (expected 9) — empty lineup slot(s).` with K below 9
and no `starter sum` warning for the same team and week. That is an owner who left a slot empty (benched an
injured player, abandoned a playoff lineup); ESPN's score is exactly what was started. The `ok` line ends with
`| empty lineup slots: wk5 team 6 (8 starters)` for weeks imported this run, or `none new (+N in stored
weeks)` afterwards (the importer re-reads every week each run). A newly imported one also gets a one-line
heads-up post.

## Waivers step (`espn_transactions_import.py`)

| Guardrail | Meaning | Check |
|---|---|---|
| dry run exit / `!!! STOPPED` | ESPN error or the importer's own stop | Last lines of the `waivers_dry` output. |
| could not read the dry run checks | Importer output changed shape (including the `[faab]` header disagreeing with its team lines) | Was the importer edited? |
| could not read faab_adjustments.json / entry N is malformed | Bad JSON or a bad entry in the manual file | Fix the file (format below). Can't be overridden. |
| ESPN returns fewer rows than stored | ESPN dropped transactions from history | Never override without finding which rows; they'd stay in the DB anyway (upserts don't delete). |
| N new rows at once (limit 150) | Unusually many moves since the last run | Missed runs, or a reprocessed season. |
| latest period before stored week | ESPN's season looks earlier than ours | Wrong season, or ESPN status lagging. |
| unresolved player ids | Players ESPN didn't name | New players; re-run later, or check `kona_player_info`. |
| waiver claims disagree with ESPN on dates | Executed claims vs ESPN's waiver process log | A claim reversed by the commissioner? Compare the dates listed in the output. |
| FAAB spent matches ESPN for only N/14 teams after trades (ESPN minus expected: …) | For some team, ESPN's spent budget ≠ its executed waiver bids + FAAB it sent in completed trades | `looks like $X from ESPN team A to ESPN team B` = an equal and opposite pair: a trade with FAAB the archived feed doesn't show (feed step failed, or the trade completed after the snapshot; next week usually clears it). Otherwise commissioner budget edits, or a missing claim. `no archived feed snapshot could be read` = check `espn_raw\activity_feed\<season>`. |
| ESPN team N has no FAAB spent figure | ESPN returned no `acquisitionBudgetSpent` | Partial ESPN response; retry. |
| draft-row truncation check failed | ESPN returned fewer DRAFT rows than `draft_picks` | Partial responses; retry. |
| post-check: rows stored vs dry run | Database doesn't match the dry run | Look at the `waivers_live` output. |

**FAAB in trades.** This league can trade FAAB. ESPN's spent budget includes it (sender up, receiver down)
but the transactions API doesn't. The wrapper reads it from the archived activity feed: a type 290 message
(from = sender, to = receiver, targetId = dollars) in a completed-trade topic (type 244), from every complete
snapshot in `espn_raw\activity_feed\<season>`. It passes only if that explains ESPN's figure exactly, for every
team. The `ok` line lists them (`| FAAB in trades: $10 ESPN team 8 -> 4 (new)`); a new one gets a heads-up post.
`espn_transactions_import.py --dry-run` on its own still prints the raw mismatch (e.g. 12/14); that is expected.

If ESPN shows a FAAB transfer the feed never will (e.g. the feed was lost), confirm it on ESPN, then add it to
`faab_adjustments.json` in the repo root (absent by default; tracked in git):

```
[{"season": 2026, "from_espn_team": 8, "to_espn_team": 4, "amount": 10, "note": "why, and where it was confirmed"}]
```

Don't add a trade the feed already shows; it would be counted twice and fail.

## Backup step (`nightly_backup.py --push`)

Runs every week, in or out of season, after the imports: a fresh dump, then a redacted plain-SQL copy
committed to `rockwood-archive` as `db/rockwood.sql`, plus a mirror of `docs\` as `docs/` (committed only
when something changed). The nightly task (*Rockwood Nightly Backup*, 03:30) does the same without the
push, and also copies `docs\` to `D:\Backup\rockwood\docs`. Details: `logs\nightly_backup.log`.

| Log line | Meaning | Check |
|---|---|---|
| `exit=1` / `dump FAIL` | pg_dump failed, or the dump lacked a required table | `BACKUP_DATABASE_URL` in `.env`; Postgres running; D: present and not full. Earlier dumps are untouched. |
| `exit=2` / `espn_raw FAIL` | robocopy returned 8+ | D: missing or full. |
| `exit=8` / `push FAIL` | Git or the secret scan failed | Git auth as for the feed step. `secret scan: dump contains the value of X` = a `.env` value is in the database: find the row, don't push. `secret scan: docs\… contains …` = a `.env` value in a docs file: remove it. `paths are ignored` = something new written to `rockwood-archive` that its allowlist `.gitignore` doesn't list. The next run retries. |
| `exit=16` / `docs FAIL` | robocopy of `docs\` to D: returned 8+ | D: missing or full. |

## Analyze step

`SELECT rockwood_analyze()` refreshes planner statistics. `rockwood_writer` can't ANALYZE tables it
doesn't own on PG 16, so the function (owned by `postgres`, SECURITY DEFINER) does it, and only
`rockwood_writer` may call it. `analyze FAIL … permission denied for function` = the grant was lost;
`function rockwood_analyze() does not exist` = it was dropped. See `docs/rockwood_reference.md`.

## Notifications

`NOTIFY_WEBHOOK_URL` gets a message when a step fails or when no run has fully succeeded in 8 days, and a
one-line heads-up when a clean run lets through a newly imported empty slot or a new trade's FAAB.
`HEALTHCHECK_URL` is pinged every run (`/fail` on failure), so its own schedule alerts you if the
task stops running altogether. Messages contain the run's log lines with every `.env` value redacted.

## Tests

`venv\Scripts\python.exe -B -m unittest discover -s tests -v` runs two suites. `test_guardrails.py` replays
captured dry runs through the guardrails offline (no DB, ESPN or notifications). `test_api_smoke.py` needs
the database: it starts the API from the working tree on a spare port as `rockwood_api` and calls every GET
route (read-only; skipped if `.env` has no `API_DATABASE_URL`). Run them after any change to `weekly_maintenance.py`, then
`venv\Scripts\python.exe -c "import weekly_maintenance, ddns_update"`, since the DDNS task imports the wrapper.
