@echo off
rem Incremental sync from Notion to the Obsidian vault.
rem Called by Task Scheduler at logon and daily at 08:00.
rem
rem ASCII only. cmd.exe reads this file in the OEM code page (cp932 here),
rem so UTF-8 Japanese in a comment is decoded as garbage and breaks parsing
rem -- the first version of this file failed that way with
rem "'...' is not recognized as an internal or external command".
rem
rem Articles keep accumulating on Render even while this does not run,
rem so a few missed days lose nothing: the next run picks up everything
rem newer than last_sync.

setlocal
set PROJECT=C:\10_Claude\20_agent\02_news-checker
set LOG=%PROJECT%\sync_task.log
set PY=C:\Users\07477\AppData\Local\Programs\Python\Python313\python.exe
set PYTHONIOENCODING=utf-8

cd /d "%PROJECT%" || exit /b 1

echo.>> "%LOG%"
echo ==== %DATE% %TIME% ====>> "%LOG%"

"%PY%" obsidian_sync.py --vault "C:\Obsidian_Vault" >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

if %RC% neq 0 (
  echo [NG] sync failed, exit code %RC%>> "%LOG%"
) else (
  echo [OK] sync finished>> "%LOG%"
)
exit /b %RC%
