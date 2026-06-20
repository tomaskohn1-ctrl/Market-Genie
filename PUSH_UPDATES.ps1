# ============================================================
#  Market Genie - push updates to GitHub / Railway
#  Right-click → Run with PowerShell
# ============================================================

Set-Location $PSScriptRoot

Write-Host ""
Write-Host "=== Clearing any stale git lock ===" -ForegroundColor Cyan
if (Test-Path ".git\index.lock") { Remove-Item ".git\index.lock" -Force }

Write-Host ""
Write-Host "=== Untracking secrets (keeps files on disk) ===" -ForegroundColor Cyan
git rm --cached --ignore-unmatch .env
git rm --cached --ignore-unmatch "__pycache__/market_genie_server.cpython-310.pyc"

Write-Host ""
Write-Host "=== Staging code changes ===" -ForegroundColor Cyan
git add market_genie_server.py strategy_engine.py dashboard.html .gitignore youtube_poster.py

Write-Host ""
Write-Host "=== Committing ===" -ForegroundColor Cyan
git commit -m "Bench UBER/MRVL/GME/DKNG/PYPL/SPY/DIA; cooldowns reduced; chop windows removed; daily loss limit -600; YouTube upload disabled"

Write-Host ""
Write-Host "=== Pushing to GitHub (Railway will auto-deploy) ===" -ForegroundColor Cyan
git push https://tomaskohn1-ctrl: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@github.com/tomaskohn1-ctrl/Market-Genie.git main

Write-Host ""
if ($LASTEXITCODE -eq 0) {
    Write-Host "=== DONE. Push succeeded — Railway is deploying now. ===" -ForegroundColor Green
} else {
    Write-Host "=== Push failed. Check the output above. ===" -ForegroundColor Red
}

Read-Host "Press Enter to close"
