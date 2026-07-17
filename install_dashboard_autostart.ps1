# install_dashboard_autostart.ps1
# Registers a Task Scheduler job that keeps the local dashboard
# (http://localhost:8770) running 24/7. Same pattern as the graduation
# sniper's task: at-logon trigger + 15-min self-heal repetition.
#
# Run once:  powershell -ExecutionPolicy Bypass -File install_dashboard_autostart.ps1
# Uninstall: powershell -ExecutionPolicy Bypass -File install_dashboard_autostart.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$TaskName  = "GradDashboard24x7"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ScriptDir "run_dashboard_forever.ps1"

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

if (-not (Test-Path $RunScript)) {
    Write-Host "[ERROR] Cannot find run_dashboard_forever.ps1 at: $RunScript" -ForegroundColor Red
    exit 1
}

Write-Host "Installing scheduled task '$TaskName'..."

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $ScriptDir

$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$TriggerHeal = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "  Replaced existing task."
} catch {}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($TriggerLogon, $TriggerHeal) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Local trading dashboard (:8770) - auto-restart watchdog" | Out-Null

Write-Host ""
Write-Host "[OK] Installed. Dashboard: http://localhost:8770" -ForegroundColor Green
Write-Host "  Start now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Uninstall:  powershell -File install_dashboard_autostart.ps1 -Uninstall"
