# run_forever.ps1
# Watchdog wrapper for pump_bot - restarts the bot on any crash.
# Logs every restart with timestamp + exit code so you can see what happened overnight.
#
# Run manually:        powershell -ExecutionPolicy Bypass -File run_forever.ps1
# Run in background:   Start-Process powershell -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File run_forever.ps1"

$ErrorActionPreference = "Continue"

# Always run from this script's directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Logs
$LogDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$WatchdogLog = Join-Path $LogDir "watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$timestamp | $Message"
    Add-Content -Path $WatchdogLog -Value $line -Encoding utf8
    Write-Host $line
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
Write-WatchdogLog "WATCHDOG STARTED - pump_bot 24/7 mode"
Write-WatchdogLog "Script dir: $ScriptDir"
Write-WatchdogLog "=========================================="

$RestartCount = 0
$MinUptimeSec = 30   # if bot crashes faster than this, back off longer

while ($true) {
    $StartTime = Get-Date
    $msg = "Launching bot (restart number " + $RestartCount + ")..."
    Write-WatchdogLog $msg

    # Run the bot - output streams live to the console
    & $PythonExe "main.py" 2>&1 | ForEach-Object { Write-Host $_ }
    $ExitCode = $LASTEXITCODE

    $UptimeSec = [int]((Get-Date) - $StartTime).TotalSeconds
    $msg = "Bot exited (code=" + $ExitCode + ") after " + $UptimeSec + "s"
    Write-WatchdogLog $msg

    # Back-off on rapid crashes so we don't hammer the API
    if ($UptimeSec -lt $MinUptimeSec) {
        $BackoffSec = [Math]::Min(300, 10 * ($RestartCount + 1))
        $msg = "Crash too fast - waiting " + $BackoffSec + "s before restart"
        Write-WatchdogLog $msg
        Start-Sleep -Seconds $BackoffSec
    } else {
        Start-Sleep -Seconds 5
    }

    $RestartCount++
}
