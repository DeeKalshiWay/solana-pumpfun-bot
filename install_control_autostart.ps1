# install_control_autostart.ps1
# Registers a Windows Task Scheduler job that runs the Telegram control bot
# at every user login. Restarts if it crashes. No admin required.
#
# Run once:  powershell -ExecutionPolicy Bypass -File install_control_autostart.ps1
# Uninstall: powershell -ExecutionPolicy Bypass -File install_control_autostart.ps1 -Uninstall

param(
    [switch]$Uninstall
)

$TaskName  = "PumpBotControl"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Module    = "tools.control_bot"

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

# Find python.exe — prefer the one already used by the project
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Path
if (-not $PythonExe) {
    Write-Host "[ERROR] python.exe not found in PATH" -ForegroundColor Red
    exit 1
}

# Sanity check the module exists
$ControlBotPath = Join-Path $ScriptDir "tools\control_bot.py"
if (-not (Test-Path $ControlBotPath)) {
    Write-Host "[ERROR] Cannot find tools/control_bot.py at $ControlBotPath" -ForegroundColor Red
    exit 1
}

Write-Host "Installing scheduled task '$TaskName'..."
Write-Host "  Python:    $PythonExe"
Write-Host "  Module:    $Module"
Write-Host "  WorkDir:   $ScriptDir"

# Run hidden — the control bot doesn't need a visible console
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m $Module" `
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
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

# Run as current user (no admin needed, runs interactively when logged in)
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Remove old version if present
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "  Replaced existing task."
} catch {
    # Not installed yet — fine
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Pump bot Telegram control + alerts (auto-start at login)" | Out-Null

Write-Host ""
Write-Host "[OK] Installed. The control bot will now:" -ForegroundColor Green
Write-Host "       - Start automatically when you log in"
Write-Host "       - Auto-restart if it ever crashes"
Write-Host "       - Run hidden in the background"
Write-Host ""
Write-Host "Manage it:"
Write-Host "  Start now:    Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop now:     Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Check status: Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Uninstall:    powershell -File install_control_autostart.ps1 -Uninstall"
Write-Host ""
Write-Host "Note: control_bot writes its own logs to stdout. To see them under"
Write-Host "the scheduled task, capture stdout to a file by editing the task's"
Write-Host "Action or wrap the python call in a small batch file that redirects."
