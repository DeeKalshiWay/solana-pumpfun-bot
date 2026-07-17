# run_dashboard_forever.ps1
# Watchdog wrapper for the local dashboard (tools/copy_dashboard.py, :8770).
# Same pattern as run_graduation_forever.ps1: named mutex against duplicate
# watchdogs, process guard against a manually-started dashboard, restart on
# exit with back-off. Logs to logs\dashboard_watchdog.log.
#
# Run manually: powershell -ExecutionPolicy Bypass -File run_dashboard_forever.ps1

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$LogDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$WatchdogLog = Join-Path $LogDir "dashboard_watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp | $Message"
    Add-Content -Path $WatchdogLog -Value $line -Encoding utf8
    Write-Host $line
}

# Guard 1 (atomic): one watchdog only
$WatchdogMutex = New-Object System.Threading.Mutex($false, "Global\GradDashboardWatchdog")
if (-not $WatchdogMutex.WaitOne(0)) {
    Write-WatchdogLog "Another dashboard watchdog holds the mutex - exiting."
    exit 0
}

# Guard 2 (belt): a dashboard started outside any watchdog
$existing = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*tools.copy_dashboard*" }
if ($existing) {
    Write-WatchdogLog "Dashboard already running (PID $($existing.ProcessId)) - exiting to avoid a duplicate."
    exit 0
}

$PythonExe = "python"
$VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"
if (Test-Path $VenvPython) { $PythonExe = $VenvPython }

Write-WatchdogLog "WATCHDOG STARTED - dashboard 24/7 mode (http://localhost:8770)"

$RestartCount = 0
$MinUptimeSec = 30

while ($true) {
    $StartTime = Get-Date
    Write-WatchdogLog ("Launching dashboard (restart number " + $RestartCount + ")...")

    & cmd /c "`"$PythonExe`" -m tools.copy_dashboard >> `"$LogDir\copy_dashboard.out`" 2>> `"$LogDir\copy_dashboard.err`""
    $ExitCode = $LASTEXITCODE

    $UptimeSec = [int]((Get-Date) - $StartTime).TotalSeconds
    Write-WatchdogLog ("Dashboard exited (code=" + $ExitCode + ") after " + $UptimeSec + "s")

    if ($UptimeSec -lt $MinUptimeSec) {
        $BackoffSec = [Math]::Min(300, 10 * ($RestartCount + 1))
        Write-WatchdogLog ("Crash too fast - waiting " + $BackoffSec + "s before restart")
        Start-Sleep -Seconds $BackoffSec
    } else {
        Start-Sleep -Seconds 5
    }

    $RestartCount++
}
