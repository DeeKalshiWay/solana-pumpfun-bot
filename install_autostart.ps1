# install_autostart.ps1
# Registers a Windows Task Scheduler job that runs pump_bot 24/7:
#   - Starts at user login (no admin needed)
#   - Restarts if it ever exits
#   - Survives reboots
#
# Run once:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1
# Uninstall: powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$TaskName  = "PumpBot24x7"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ScriptDir "run_forever.ps1"

if ($Uninstall) {
    Write-Host "Removing scheduled task '$TaskName'..."
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "[OK] Task removed." -ForegroundColor Green
    } catch {
        Write-Host "[!] Task not found or could not be removed: $_" -ForegroundColor Yellow
    }
    exit 0
}

# Sanity check
if (-not (Test-Path $RunScript)) {
    Write-Host "[ERROR] Cannot find run_forever.ps1 at: $RunScript" -ForegroundColor Red
    exit 1
}

Write-Host "Installing scheduled task '$TaskName'..."
Write-Host "  Script: $RunScript"

# Run hidden via powershell.exe
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $ScriptDir

# Trigger: at user logon
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: keep alive, restart on failure, no time limit
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 days = no limit

# Run as current user, with stored credentials so it works when locked
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Remove old version if present
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "  Replaced existing task."
} catch {
    # Not installed yet - that's fine
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Pump.fun trading bot - auto-restart watchdog" | Out-Null

Write-Host ""
Write-Host "[OK] Installed. The bot will:" -ForegroundColor Green
Write-Host "       - Start automatically when you log in"
Write-Host "       - Auto-restart if it ever crashes"
Write-Host "       - Run hidden in the background"
Write-Host ""
Write-Host "Manage it:"
Write-Host "  Start now:     Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop now:      Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Check status:  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Uninstall:     powershell -File install_autostart.ps1 -Uninstall"
Write-Host ""
Write-Host "Logs:"
Write-Host "  Watchdog:      logs\watchdog.log"
Write-Host "  Bot output:    logs\pump_bot.log"
Write-Host "  Trades:        logs\trades.log"
