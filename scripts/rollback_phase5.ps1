# Rolls back audit Phase 5 (frontend polish, 2026-11). Frontend and Caddyfile only: no API or
# database change was made in Phase 5. Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\projects\rockwood\scripts\rollback_phase5.ps1
#
# 1. rockwood-ui back to the pre-phase5 tag; Phase 5 commits kept on branch phase5-rolled-back,
#    uncommitted changes stashed
# 2. rockwood-ui\dist replaced by dist-pre-phase5 (the exact pre-Phase-5 build): that alone restores
#    the live site. Then node_modules from the restored lockfile (recharts comes back), only so
#    npm run dev works; it runs under npm 11 because the system npm 10.8.2 crashes on installs
# 3. Caddyfile restored from its pre-Phase-5 backup (the CSP allows Google Fonts again), validated,
#    then caddy stopped and started
# 4. both sites checked
# No passwords or URLs with secrets here.

$ErrorActionPreference = 'Stop'
$ui      = 'C:\projects\rockwood-ui'
$caddy   = 'C:\caddy\caddy.exe'
$cfg     = 'C:\caddy\Caddyfile'
$cfgBak  = 'C:\caddy\Caddyfile.bak-20261110-prephase5'

foreach ($p in @("$ui\dist-pre-phase5\index.html", $cfgBak)) {
	if (-not (Test-Path $p)) { throw "missing $p; nothing changed" }
}
& git -C $ui rev-parse --verify --quiet pre-phase5 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "tag pre-phase5 missing in $ui; nothing changed" }
# validate the backup before touching anything, so a bad backup stops the rollback here
& $caddy validate --config $cfgBak --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw "the Caddyfile backup doesn't validate; nothing changed" }

Write-Host '== 1. repo'
& git -C $ui stash push -u -m 'uncommitted at Phase 5 rollback' | Out-Null
& git -C $ui branch -f phase5-rolled-back HEAD
& git -C $ui reset --hard pre-phase5
& git -C $ui log --oneline -1

Write-Host '== 2. frontend'
Remove-Item "$ui\dist" -Recurse -Force
Copy-Item "$ui\dist-pre-phase5" "$ui\dist" -Recurse
Push-Location $ui
& npx -y npm@11 ci --no-audit --no-fund
if ($LASTEXITCODE -ne 0) { Write-Warning 'npm ci failed: the site is restored, only npm run dev needs node_modules' }
Pop-Location

Write-Host '== 3. Caddy'
Copy-Item $cfg "$cfg.phase5-rolled-back" -Force
Copy-Item $cfgBak $cfg -Force
& $caddy validate --config $cfg --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'restored Caddyfile does not validate (the Phase 5 one is at Caddyfile.phase5-rolled-back); caddy not restarted' }
& nssm stop caddy | Out-Null
& nssm start caddy | Out-Null
Start-Sleep 3
(Get-Service caddy).Status

Write-Host '== 4. checks'
foreach ($h in @('theleague', 'thebar')) {
	& curl.exe -s -o NUL -w "  https://$h.letsfriggen.party/ -> %{http_code}`n" "https://$h.letsfriggen.party/"
}
$csp = (& curl.exe -sI https://theleague.letsfriggen.party/ | Select-String -Pattern '^content-security-policy:').Line
Write-Host "  CSP allows Google Fonts again: $($csp -match 'fonts.googleapis.com')"
Write-Host 'Done. Phase 5 commits are on branch phase5-rolled-back in rockwood-ui.'
