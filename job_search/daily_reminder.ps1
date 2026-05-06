# daily_reminder.ps1
# Pops a Windows notification + opens 5 pre-tuned LinkedIn / job board searches.
# Designed to take applying for jobs from "0 friction wall of decisions" to "click,
# read, click apply" in under 60 seconds.
#
# Triggered by Task Scheduler daily at 09:00 (see install_job_reminder.ps1).

$ErrorActionPreference = "Continue"

# ── Notification ───────────────────────────────────────────────────────────
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.BalloonTipIcon = "Info"
$notify.BalloonTipTitle = "Daily job apply: do 3 today"
$notify.BalloonTipText = "Opening 5 search tabs. Apply to 3, log them in applications.md. Takes 15 min."
$notify.ShowBalloonTip(15000)

# Keep notification alive for a moment
Start-Sleep -Seconds 5
$notify.Dispose()

# ── Open targeted job searches ─────────────────────────────────────────────
# These queries are tuned to roles that match what you've actually built:
# Python + Solana + RPC integration + trading systems + dashboards.
$searches = @(
    # Highest-paying matches first
    "https://www.linkedin.com/jobs/search/?keywords=Solana%20Python%20Engineer&f_WT=2&sortBy=DD",
    "https://www.linkedin.com/jobs/search/?keywords=Trading%20Bot%20Developer%20Python&f_WT=2&sortBy=DD",
    "https://www.linkedin.com/jobs/search/?keywords=Web3%20Backend%20Engineer%20Python&f_WT=2&sortBy=DD",
    "https://www.linkedin.com/jobs/search/?keywords=Quantitative%20Developer%20Crypto&f_WT=2&sortBy=DD",
    # Broader fallback
    "https://www.linkedin.com/jobs/search/?keywords=Python%20Developer%20Remote&f_WT=2&sortBy=DD"
)

foreach ($url in $searches) {
    Start-Process $url
    Start-Sleep -Milliseconds 500
}

# ── Open the application tracker so it's right in front of you ────────────
$tracker = "C:\Users\denni\Downloads\pump_bot\pump_bot\job_search\applications.md"
if (Test-Path $tracker) {
    Start-Process notepad.exe -ArgumentList $tracker
}

# Log the reminder fire so you can see your own streak
$logFile = "C:\Users\denni\Downloads\pump_bot\pump_bot\job_search\reminder.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "$timestamp | reminder fired"
