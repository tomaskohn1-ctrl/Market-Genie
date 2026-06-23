# ============================================================
#  Market Genie - push updates to GitHub / Railway
#  Right-click → Run with PowerShell
# ============================================================
$RepoDir = "C:\Users\tomas\Documents\Claude\Projects\Market Genie"
$LogFile = "C:\Users\tomas\Documents\Claude\Projects\Market Genie\push_log.txt"
Start-Transcript -Path $LogFile -Force

Set-Location $RepoDir

Write-Host ""
Write-Host "=== Closing GitHub Desktop ===" -ForegroundColor Cyan
Get-Process -Name "GitHubDesktop" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Host ""
Write-Host "=== Clearing any stale git lock ===" -ForegroundColor Cyan
if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -Force; Write-Host "Lock removed." }

Write-Host ""
Write-Host "=== Untracking secrets (keeps files on disk) ===" -ForegroundColor Cyan
git rm --cached --ignore-unmatch .env
git rm --cached --ignore-unmatch "__pycache__/market_genie_server.cpython-310.pyc"

Write-Host ""
Write-Host "=== Staging code changes ===" -ForegroundColor Cyan
git add market_genie_server.py strategy_engine.py dashboard.html .gitignore youtube_poster.py

Write-Host ""
Write-Host "=== Committing ===" -ForegroundColor Cyan
git commit -m "Zero all cooldowns; bench UBER/MRVL/GME/DKNG/PYPL/SPY/DIA; opening guard off; YouTube upload off (Jun 23)"

Write-Host ""
Write-Host "=== Pushing to GitHub (Railway will auto-deploy) ===" -ForegroundColor Cyan
git push origin main

Write-Host ""
if ($LASTEXITCODE -eq 0) {
    Write-Host "=== DONE. Push succeeded — Railway is deploying now. ===" -ForegroundColor Green
} else {
    Write-Host "=== Push failed. Check the output above. ===" -ForegroundColor Red
}

Stop-Transcript
Write-Host "Log saved to: $LogFile" -ForegroundColor Yellow
Read-Host "Press Enter to close"
