@echo off
rem Runs the app with a console so any error text is visible.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m facturedz) || (python -m facturedz)
pause
