# Rolls back audit Phase 3 (2026-10-22). Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\projects\rockwood\scripts\rollback_phase3.ps1
#
# 1. database: rollback_phase3.sql, then draft_order and box_scores_backup_20261007 restored from
#    the pre-phase3 dump (filtered pg_restore)
# 2. both repos back to the pre-phase3 tag; Phase 3 commits kept on branch phase3-rolled-back,
#    uncommitted changes stashed
# 3. rockwood-ui\dist replaced by dist-pre-phase3 (no rebuild needed), node_modules from the
#    restored lockfile (axios comes back) so npm run dev works
# 4. rockwood-api restarted, then checked
# Uses psql/pg_restore as postgres through %APPDATA%\postgresql\pgpass.conf; no passwords here.

$ErrorActionPreference = 'Stop'
$api   = 'C:\projects\rockwood'
$ui    = 'C:\projects\rockwood-ui'
$pgbin = 'C:\Program Files\PostgreSQL\16\bin'
$dump  = 'D:\Backup\rockwood\db\rockwood_pre_phase3.dump'

foreach ($p in @($dump, "$ui\dist-pre-phase3\index.html")) {
	if (-not (Test-Path $p)) { throw "missing $p; nothing changed" }
}
foreach ($r in @($api, $ui)) {
	& git -C $r rev-parse --verify --quiet pre-phase3 | Out-Null
	if ($LASTEXITCODE -ne 0) { throw "tag pre-phase3 missing in $r; nothing changed" }
}

# The SQL file is removed from the working tree by step 2, so copy it first
$sql = Join-Path $env:TEMP 'rollback_phase3.sql'
Copy-Item "$api\scripts\rollback_phase3.sql" $sql -Force

Write-Host '== 1. database'
& "$pgbin\psql.exe" -h localhost -U postgres -d rockwood -X -f $sql
if ($LASTEXITCODE -ne 0) { throw 'rollback_phase3.sql failed (it runs in one transaction, so nothing was applied)' }
$list = Join-Path $env:TEMP 'rollback_phase3.list'
& "$pgbin\pg_restore.exe" -l $dump | Select-String -Pattern 'draft_order|box_scores_backup_20261007' |
	ForEach-Object { $_.Line } | Set-Content -Encoding ascii $list
& "$pgbin\pg_restore.exe" -h localhost -U postgres -d rockwood -L $list $dump
if ($LASTEXITCODE -ne 0) { throw 'pg_restore of the dropped tables failed' }

Write-Host '== 2. repos'
foreach ($r in @($api, $ui)) {
	& git -C $r stash push -u -m 'uncommitted at Phase 3 rollback' | Out-Null
	& git -C $r branch -f phase3-rolled-back HEAD
	& git -C $r reset --hard pre-phase3
	& git -C $r log --oneline -1
}

Write-Host '== 3. frontend'
Remove-Item "$ui\dist" -Recurse -Force
Copy-Item "$ui\dist-pre-phase3" "$ui\dist" -Recurse
Push-Location $ui
& npm ci --no-audit --no-fund
Pop-Location

Write-Host '== 4. API'
& nssm restart rockwood-api | Out-Null
Start-Sleep 5
(Get-Service rockwood-api).Status
& curl.exe -s -o NUL -w "  /api/records/alltime -> %{http_code} (exists again after rollback)`n" https://theleague.letsfriggen.party/api/records/alltime
& "$pgbin\psql.exe" -h localhost -U postgres -d rockwood -Atc "SELECT 'draft_order rows: ' || count(*) FROM draft_order UNION ALL SELECT 'team_games view gone: ' || (to_regclass('team_games') IS NULL)::text"
Write-Host 'Done. Phase 3 commits are on branch phase3-rolled-back in both repos.'
