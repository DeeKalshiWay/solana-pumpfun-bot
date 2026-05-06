# install_job_reminder.ps1
# Registers a Windows Task Scheduler entry that runs daily_reminder.ps1
# at 09:00 every day. No admin required.
#
# Run once:    powershell -ExecutionPolicy Bypass -File install_job_reminder.ps1
# Uninstall:   powershell -ExecutionPolicy Bypass -File install_job_reminder.ps1 -Uninstall

param([switch]$Uninstall)

$TaskName  = "DailyJobApplyReminder"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Reminder  = Join-Path $ScriptDir "daily_reminder.ps1"

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "[OK] Reminder removed." -ForegroundColor Green
    } catch {
        Write-Host "[!] Reminder not found." -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $Reminder)) {
    Write-Host "[ERROR] daily_reminder.ps1 not found." -ForegroundColor Red
    exit 1
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Reminder`"" `
    -WorkingDirectory $ScriptDir

# Daily at 09:00. Change to your preferred time if you want.
$Trigger = New-ScheduledTaskTrigger -Daily -At "09:00"

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Replace if exists
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Daily reminder + 5 LinkedIn job searches at 09:00" | Out-Null

Write-Host ""
Write-Host "[OK] Daily job reminder installed." -ForegroundColor Green
Write-Host "  Fires at:  09:00 every day"
Write-Host "  Pops Windows notification + opens 5 LinkedIn searches + opens applications.md"
Write-Host ""
Write-Host "Test now:    Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop:        Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Uninstall:   powershell -File install_job_reminder.ps1 -Uninstall"
