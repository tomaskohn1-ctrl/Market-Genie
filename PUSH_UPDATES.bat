@echo off
REM ============================================================
REM  Market Genie - push strategy tracker + lock down .env
REM  Double-click this file to commit and push to GitHub/Railway.
REM ============================================================
cd /d "%~dp0"

echo.
echo === Clearing any stale git lock ===
if exist ".git\index.lock" del /f /q ".git\index.lock"

echo.
echo === Untracking secrets (keeps the files on disk) ===
git rm --cached --ignore-unmatch .env
git rm --cached --ignore-unmatch "__pycache__/market_genie_server.cpython-310.pyc"

echo.
echo === Staging code changes ===
git add market_genie_server.py strategy_engine.py dashboard.html .gitignore

echo.
echo === Committing ===
git commit -m "Add /api/strat/status tracker, $780 goal panel, gate stats; untrack .env"

echo.
echo === Pushing to GitHub (Railway will auto-deploy) ===
git push origin main

echo.
echo === DONE. Review the output above. ===
echo If you see 'main -^> main', the push succeeded.
pause
