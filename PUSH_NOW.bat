@echo off
cd /d "C:\Users\tomas\Documents\Claude\Projects\Market Genie"

echo === Finding git ===
where git 2>nul
if %errorlevel% neq 0 (
    echo git not in PATH - trying GitHub Desktop location...
    set GIT="C:\Users\tomas\AppData\Local\GitHubDesktop\app-3.4.3\resources\app\git\cmd\git.exe"
) else (
    set GIT=git
)

echo === Closing GitHub Desktop ===
taskkill /f /im GitHubDesktop.exe 2>nul
timeout /t 2 /nobreak >nul

echo === Clearing git locks ===
if exist ".git\index.lock" del /f /q ".git\index.lock" && echo index.lock removed.
if exist ".git\HEAD.lock" del /f /q ".git\HEAD.lock" && echo HEAD.lock removed.

echo === Staging changes ===
%GIT% add market_genie_server.py strategy_engine.py youtube_poster.py PUSH_UPDATES.ps1

echo === Committing ===
%GIT% commit -m "Zero all cooldowns; bench UBER/MRVL/GME/DKNG/PYPL/SPY/DIA; opening guard off; YouTube upload off (Jun 23)"

echo === Pushing to GitHub ===
%GIT% push https://tomaskohn1-ctrl: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@github.com/tomaskohn1-ctrl/Market-Genie.git main

echo.
echo === DONE - check output above ===
pause
