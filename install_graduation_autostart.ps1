# install_graduation_autostart.ps1
# Registers a Windows Task Scheduler job that runs the graduation sniper 24/7:
#   - Starts at user login (no admin needed)
#   - Restarts if it ever exits
#   - Survives reboots (the July 12 Windows Update reboot killed a 4-day run)
#
# Run once:  powershell -ExecutionPolicy Bypass -File install_graduation_autostart.ps1
# Uninstall: powershell -ExecutionPolicy Bypass -File install_graduation_autostart.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$TaskName  = "GraduationSniper24x7"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ScriptDir "run_graduation_forever.ps1"

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
    Write-Host "[ERROR] Cannot find run_graduation_forever.ps1 at: $RunScript" -ForegroundColor Red
    exit 1
}

Write-Host "Installing scheduled task '$TaskName'..."
Write-Host "  Script: $RunScript"

# Run hidden via powershell.exe
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $ScriptDir

# Triggers: at user logon, PLUS a 15-min self-heal repetition. If the
# watchdog process itself ever dies (task killed, powershell crash), the
# repetition relaunches it within 15 min; the watchdog's duplicate guard
# makes the extra fires no-ops while everything is healthy, and Task
# Scheduler's default IgnoreNew policy skips fires while an instance runs.
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$TriggerHeal = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Trigger = @($TriggerLogon, $TriggerHeal)

# Settings: keep alive, restart on failure, no time limit
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 days = no limit

# Run as current user
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
    -Description "Pump.fun graduation sniper (paper) - auto-restart watchdog" | Out-Null

Write-Host ""
Write-Host "[OK] Installed. The sniper will:" -ForegroundColor Green
Write-Host "       - Start automatically when you log in"
Write-Host "       - Auto-restart if it ever crashes"
Write-Host "       - Run hidden in the background"
Write-Host ""
Write-Host "Manage it:"
Write-Host "  Start now:     Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop now:      Stop-ScheduledTask -TaskName $TaskName  (then create logs\KILL_GRADUATION or kill python)"
Write-Host "  Check status:  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Uninstall:     powershell -File install_graduation_autostart.ps1 -Uninstall"
Write-Host ""
Write-Host "Logs:"
Write-Host "  Watchdog:      logs\graduation_watchdog.log"
Write-Host "  Sniper output: logs\graduation_sniper.out"
Write-Host "  Trades:        logs\graduation_trades.jsonl"
