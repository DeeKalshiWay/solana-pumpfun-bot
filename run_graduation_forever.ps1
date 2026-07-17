# run_graduation_forever.ps1
# Watchdog wrapper for the graduation sniper - restarts it on any crash.
# Same pattern as run_forever.ps1 (main bot). Logs restarts with timestamp
# + exit code to logs\graduation_watchdog.log; sniper output appends to
# logs\graduation_sniper.out / .err so grad_report.py keeps working.
#
# Run manually:        powershell -ExecutionPolicy Bypass -File run_graduation_forever.ps1
# Run in background:   Start-Process powershell -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File run_graduation_forever.ps1"

$ErrorActionPreference = "Continue"

# Always run from this script's directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Logs
$LogDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$WatchdogLog = Join-Path $LogDir "graduation_watchdog.log"
$KillFile    = Join-Path $LogDir "KILL_GRADUATION"

function Write-WatchdogLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp | $Message"
    Add-Content -Path $WatchdogLog -Value $line -Encoding utf8
    Write-Host $line
}

# Guard: if a sniper is already running (started manually or by another
# watchdog), don't double-trade the paper book.
$existing = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*tools.graduation_sniper*" }
if ($existing) {
    Write-WatchdogLog "Sniper already running (PID $($existing.ProcessId)) - exiting to avoid a duplicate."
    exit 0
}

# Find Python (prefer venv if present, else system Python)
$PythonExe = "python"
$VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonExe = $VenvPython
    Write-WatchdogLog "Using venv Python: $VenvPython"
} else {
    Write-WatchdogLog "Using system Python"
}

Write-WatchdogLog "=========================================="
Write-WatchdogLog "WATCHDOG STARTED - graduation sniper 24/7 mode"
Write-WatchdogLog "Script dir: $ScriptDir"
Write-WatchdogLog "=========================================="

$RestartCount = 0
$MinUptimeSec = 30   # if the sniper crashes faster than this, back off longer

while ($true) {
    # Respect the kill switch: idle instead of restart-looping against it.
    if (Test-Path $KillFile) {
        Write-WatchdogLog "KILL_GRADUATION present - idling 60s (remove the file to resume)"
        Start-Sleep -Seconds 60
        continue
    }

    $StartTime = Get-Date
    Write-WatchdogLog ("Launching sniper (restart number " + $RestartCount + ")...")

    # cmd handles the append-redirects so stderr isn't wrapped in ErrorRecords
    & cmd /c "`"$PythonExe`" -m tools.graduation_sniper >> `"$LogDir\graduation_sniper.out`" 2>> `"$LogDir\graduation_sniper.err`""
    $ExitCode = $LASTEXITCODE

    $UptimeSec = [int]((Get-Date) - $StartTime).TotalSeconds
    Write-WatchdogLog ("Sniper exited (code=" + $ExitCode + ") after " + $UptimeSec + "s")

    # Back-off on rapid crashes so we don't hammer the API
    if ($UptimeSec -lt $MinUptimeSec) {
        $BackoffSec = [Math]::Min(300, 10 * ($RestartCount + 1))
        Write-WatchdogLog ("Crash too fast - waiting " + $BackoffSec + "s before restart")
        Start-Sleep -Seconds $BackoffSec
    } else {
        Start-Sleep -Seconds 5
    }

    $RestartCount++
}
