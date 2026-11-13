# Rolls back the Tower overhaul (2026-11-12). Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\projects\rockwood\scripts\rollback_tower_overhaul.ps1
#
# 1. app\question_bank.py and app\routers\tower.py back to tag pre-tower-overhaul (rockwood), and
#    src\pages\Tower.jsx back to tag pre-tower-overhaul (rockwood-ui). Left as uncommitted changes;
#    commit them if the rollback is to stay. The shared-query refactor in teams.py and transactions.py
#    stays: the old question bank doesn't use it and the routes' output is unchanged.
# 2. database: scripts\rollback_tower_overhaul.sql as postgres (drops tower_daily and the two keep-best
#    indexes, revokes UPDATE on tower_scores). Scores stay one row per player per board.
# 3. API restarted, frontend rebuilt (Caddy serves dist\ directly)
# 4. checks
# tests\test_tower_questions.py and tests\test_tower_backend.py test the overhaul and fail after this.
# No passwords here: psql reads %APPDATA%\postgresql\pgpass.conf.

$ErrorActionPreference = 'Stop'
$api  = 'C:\projects\rockwood'
$ui   = 'C:\projects\rockwood-ui'
$psql = 'C:\Program Files\PostgreSQL\16\bin\psql.exe'

foreach ($repo in @($api, $ui)) {
	& git -C $repo rev-parse --verify --quiet pre-tower-overhaul | Out-Null
	if ($LASTEXITCODE -ne 0) { throw "tag pre-tower-overhaul missing in $repo; nothing changed" }
}
if (-not (Test-Path $psql)) { throw "psql not found at $psql; nothing changed" }

Write-Host '== 1. code'
& git -C $api checkout pre-tower-overhaul -- app/question_bank.py app/routers/tower.py
& git -C $ui checkout pre-tower-overhaul -- src/pages/Tower.jsx
& git -C $api status --short
& git -C $ui status --short

Write-Host '== 2. database'
& $psql -h localhost -U postgres -d rockwood -w -v ON_ERROR_STOP=1 -f "$api\scripts\rollback_tower_overhaul.sql"
if ($LASTEXITCODE -ne 0) { throw 'rollback SQL failed (it runs in one transaction, so nothing in the database changed); code files are restored but the API was not restarted' }

Write-Host '== 3. restart and rebuild'
& nssm restart rockwood-api | Out-Null
Push-Location $ui
& npm run build
if ($LASTEXITCODE -ne 0) { Write-Warning 'build failed: fix and rerun npm run build in rockwood-ui' }
Pop-Location
Start-Sleep 3

Write-Host '== 4. checks'
& curl.exe -s -o NUL -w "  /api/tower/round -> %{http_code}`n" 'https://theleague.letsfriggen.party/api/tower/round'
& curl.exe -s -o NUL -w "  /api/tower/daily -> %{http_code}`n" 'https://theleague.letsfriggen.party/api/tower/daily'
& curl.exe -s -o NUL -w "  /tower -> %{http_code}`n" 'https://theleague.letsfriggen.party/tower'
Write-Host 'Done. Commit the restored files in both repos if the rollback is to stay.'
