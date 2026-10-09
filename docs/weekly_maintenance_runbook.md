# Weekly maintenance runbook

`weekly_maintenance.py` runs every Wednesday at 08:00 (Task Scheduler: *Rockwood Weekly Maintenance*).
Each run writes one line per step to `weekly_maintenance.log`; the full output of every dry and
live run is in `maintenance_output/<time>_<step>_<dry|live|run>.txt`. Start there.

A tripped guardrail writes **nothing** for that step. The next Wednesday re-checks from scratch,
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
| dry run reported warnings | e.g. a matchup with an unknown team id | The WARNINGS section of the output; `espn_team_map` for the season. |
| team map N teams, expected M | Team count changed | A team added or removed in ESPN? `espn_team_map` rows. |
| completed weeks not contiguous | A gap in decided weeks | ESPN schedule for that week; a postponed game? |
| fewer completed weeks than stored | ESPN reports a decided week as undecided again | ESPN re-scoring after a correction; wait a day and dry-run again. |
| N new weeks at once | More than 2 weeks since the last import | The task missed runs (check Task Scheduler history). Override if the weeks look right. |
| week marked complete but current period is N | Importing a week still being played | ESPN `status`; don't override during a live week. |
| wkN: M matchups, expected 7 | Regular-season week with the wrong number of games | ESPN schedule; a team missing from the map. |
| wkN: starters, expected about 126 | Many empty starting slots | Owners leaving lineups empty, or ESPN returning partial rosters. Compare with ESPN's site. |
| wkN: rows vs stored median | A new week's roster count is far from normal | Partial ESPN response; roster size change mid-season. |
| wkN (playoffs): matchups | Playoff week outside 1–7 games | Playoff settings changed? |
| stored rows missing from ESPN | ESPN no longer returns players we stored | Player id changes or a partial response. Don't override without comparing with ESPN's site. |
| lineup changed after the fact | Stored starters now benched, or new starters in a scored week | Late ESPN lineup corrections. Verify on ESPN, then override. |
| point changes in older weeks / more than 40 | Stat corrections beyond the latest week | NFL stat corrections can land late; check a couple of players on ESPN, then override. |
| draft: ESPN N picks, stored M | Draft data changed | Keeper or pick edits by the commissioner. |
| post-check: … differ | Database doesn't match the dry run after writing | Look at the `scores_live` output; re-run the dry run to compare. |

## Waivers step (`espn_transactions_import.py`)

| Guardrail | Meaning | Check |
|---|---|---|
| dry run exit / `!!! STOPPED` | ESPN error or the importer's own stop | Last lines of the `waivers_dry` output. |
| could not read the dry run checks | Importer output changed shape | Was the importer edited? |
| ESPN returns fewer rows than stored | ESPN dropped transactions from history | Never override without finding which rows; they'd stay in the DB anyway (upserts don't delete). |
| N new rows at once (limit 150) | Unusually many moves since the last run | Missed runs, or a reprocessed season. |
| latest period before stored week | ESPN's season looks earlier than ours | Wrong season, or ESPN status lagging. |
| unresolved player ids | Players ESPN didn't name | New players; re-run later, or check `kona_player_info`. |
| waiver claims disagree with ESPN on dates | Executed claims vs ESPN's waiver process log | A claim reversed by the commissioner? Compare the dates listed in the output. |
| FAAB matches for only N/14 teams | Stored bids don't add up to ESPN's spent budget | Commissioner budget edits, or a missing claim. |
| draft-row truncation check failed | ESPN returned fewer DRAFT rows than `draft_picks` | Partial responses; retry. |
| post-check: rows stored vs dry run | Database doesn't match the dry run | Look at the `waivers_live` output. |

## Notifications

`NOTIFY_WEBHOOK_URL` gets a message when a step fails or when no run has fully succeeded in 8 days.
`HEALTHCHECK_URL` is pinged every run (`/fail` on failure), so its own schedule alerts you if the
task stops running altogether. Messages contain the run's log lines with every `.env` value redacted.
