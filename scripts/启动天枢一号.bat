@echo off
REM Harness is the main professional entry. Legacy UI: start-legacy.bat.
node "%~dp0start-harness.js" %*
exit /b %errorlevel%
