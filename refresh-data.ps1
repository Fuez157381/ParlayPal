# Nightly data refresh for Parlay Pal. Windows Task Scheduler runs this (task "Parlay Pal data refresh").
# It saves fresh NBA data on this PC (the NBA blocks cloud servers like Vercel), then uploads the
# snapshots folder to GitHub, and Vercel redeploys the live site by itself.
# What happened each run is written to snapshot-log.txt in this folder.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = 'C:\Users\gmach\AppData\Local\Python\pythoncore-3.15-64\python.exe'
$git = 'C:\Program Files\Git\cmd\git.exe'
$log = Join-Path $here 'snapshot-log.txt'
Set-Location $here

function Log($t) { Add-Content -Path $log -Value $t -Encoding utf8 }
Log "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="

# try up to 3 times, since the NBA sometimes answers slowly
$ok = $false
for ($i = 1; $i -le 3 -and -not $ok; $i++) {
    $out = & $py make_snapshot.py 2>&1 | Out-String
    Log $out.Trim()
    $ok = ($LASTEXITCODE -eq 0) -and ($out -match 'saved')
    if (-not $ok) { Log "try $i failed"; Start-Sleep -Seconds 120 }
}
if (-not $ok) { Log 'gave up: NBA data could not be saved'; exit 1 }

& $git add -A snapshots 2>&1 | Out-Null
& $git diff --cached --quiet -- snapshots
if ($LASTEXITCODE -eq 0) { Log 'no changes to upload'; exit 0 }
$msg = "Nightly data refresh $(Get-Date -Format 'yyyy-MM-dd HH:mm')`n`nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
& $git commit -q -m $msg -- snapshots 2>&1 | ForEach-Object { Log $_ }
& $git push -q origin main 2>&1 | ForEach-Object { Log $_ }
if ($LASTEXITCODE -eq 0) { Log 'uploaded' } else { Log 'upload failed' ; exit 1 }

# keep the log short
$lines = Get-Content $log
if ($lines.Count -gt 400) { $lines | Select-Object -Last 300 | Set-Content $log -Encoding utf8 }
