# auto_update_chart.ps1
# Snapshots the live bot state and pushes to GitHub Pages every 5 minutes.
# Result: the public portfolio page shows ~5-min-fresh data without needing
# a live tunnel.
#
# Run hidden in background:
#   Start-Process powershell -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File auto_update_chart.ps1"
#
# Or just leave a regular PowerShell window running it.

$ErrorActionPreference = "Continue"
$RepoDir   = "C:\Users\denni\Downloads\pump_bot\pump_bot"
$DataFile  = Join-Path $RepoDir "docs\equity_data.json"
$LogFile   = Join-Path $RepoDir "logs\chart_updater.log"
$IntervalS = 300   # 5 minutes between pushes

Set-Location $RepoDir

function Log {
    param([string]$msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $msg"
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

Log "Chart auto-updater started (interval ${IntervalS}s)"

while ($true) {
    try {
        # 1. Regenerate the snapshot from current bot state
        $py = @"
import json, time, urllib.request, os
snaps = []
try:
    with open('logs/report.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try: snaps.append(json.loads(line))
                except: pass
except Exception:
    pass
try:
    with urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=4) as r:
        live = json.loads(r.read())
    snaps.append({
        'ts':            int(time.time()),
        'balance_sol':   live['balance_sol'],
        'starting_sol':  1.0,
        'pnl_pct':       live.get('total_pnl_pct', 0),
        'closed_trades': live['closed_trades'],
    })
except Exception as e:
    pass
clean = [{'ts': s.get('ts'),
          'balance_sol':   s.get('balance_sol'),
          'starting_sol':  s.get('starting_sol', 1.0),
          'pnl_pct':       s.get('pnl_pct', 0),
          'closed_trades': s.get('closed_trades', 0)} for s in snaps]
if len(clean) > 200:
    step = len(clean) // 200
    clean = clean[::step] + [clean[-1]]
    seen, out = set(), []
    for s in clean:
        if s['ts'] not in seen:
            out.append(s); seen.add(s['ts'])
    clean = sorted(out, key=lambda x: x['ts'])
data = {'snapshots': clean, 'generated_at': int(time.time())}
with open('docs/equity_data.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)
print(f'snapshots={len(clean)}')
"@
        $result = python -c $py
        Log "Snapshot regenerated: $result"

        # 2. git add / commit / push (only if file actually changed)
        $diff = git diff --quiet docs/equity_data.json; $changed = $LASTEXITCODE -ne 0
        if ($changed) {
            git add docs/equity_data.json | Out-Null
            git -c user.email="bot@local" -c user.name="auto-updater" commit -m "auto: refresh equity chart" -q | Out-Null
            $push = git push origin main 2>&1
            if ($LASTEXITCODE -eq 0) {
                Log "Pushed to GitHub"
            } else {
                Log "Push failed: $push"
            }
        } else {
            Log "No data change, skip push"
        }
    } catch {
        Log "Cycle error: $_"
    }
    Start-Sleep -Seconds $IntervalS
}
