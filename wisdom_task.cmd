@echo off
rem Daily check for new Wisdom-Beta articles.
rem Collects new articles, writes commentary into the new-articles folder under
rem C:\Obsidian_Vault\Wisdom-Beta, and updates the table of contents.
rem Called by Task Scheduler at logon and daily at 08:30.
rem ASCII only: cmd.exe reads this file in cp932, so Japanese here would break parsing.

setlocal
set PROJECT=C:\10_Claude\20_agent\02_news-checker
set LOG=%PROJECT%\wisdom_task.log
set PY=C:\Users\07477\AppData\Local\Programs\Python\Python313\python.exe
set PYTHONIOENCODING=utf-8

cd /d "%PROJECT%" || exit /b 1

echo.>> "%LOG%"
echo ==== %DATE% %TIME% ====>> "%LOG%"

"%PY%" -u wisdom_beta.py daily >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC% neq 0 (
  echo [NG] wisdom daily failed, exit code %RC%>> "%LOG%"
) else (
  echo [OK] wisdom daily finished>> "%LOG%"
)
exit /b %RC%
