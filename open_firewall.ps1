# open_firewall.ps1
# Opens Windows Firewall on TCP 8765 so you can hit the dashboard from your phone
# (or any other device on the same Wi-Fi).
#
# REQUIRES ADMIN. Right-click PowerShell -> "Run as administrator", then:
#   powershell -ExecutionPolicy Bypass -File open_firewall.ps1
#
# To remove the rule later:
#   powershell -ExecutionPolicy Bypass -File open_firewall.ps1 -Remove

param([switch]$Remove)

$RuleName = "PumpBot Dashboard 8765"

if ($Remove) {
    try {
        Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction Stop
        Write-Host "[OK] Firewall rule removed." -ForegroundColor Green
    } catch {
        Write-Host "[!] Rule not found." -ForegroundColor Yellow
    }
    exit 0
}

# Check admin
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Host "[ERROR] This script requires admin. Right-click PowerShell -> Run as Administrator." -ForegroundColor Red
    exit 1
}

# Remove old rule if it exists
Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue

New-NetFirewallRule `
    -DisplayName $RuleName `
    -Direction Inbound `
    -Protocol TCP `
    -LocalPort 8765 `
    -Action Allow `
    -Profile Private `
    -Description "Allow LAN access to pump bot dashboard" | Out-Null

Write-Host "[OK] Firewall rule added: TCP 8765 inbound (Private network only)" -ForegroundColor Green
Write-Host ""
Write-Host "The dashboard is now reachable from your phone on the same Wi-Fi."
Write-Host "Find your PC's LAN IP with:  ipconfig | findstr IPv4"
Write-Host "Then on your phone, open:    http://YOUR_PC_IP:8765"
