@echo off
setlocal enabledelayedexpansion
title TianShu-1 Distribution Power Flow Agent

REM ---- resolve directories from this script's location ----
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
for %%I in ("%APP_DIR%\..") do set "ROOT=%%~fI"
set "LAUNCH_JS=%APP_DIR%\launch.js"
set "NODE_EXE="

REM ---- clear host-injected vars that break Electron ----
set NODE_OPTIONS=
set ELECTRON_RUN_AS_NODE=
set ELECTRON_NO_ATTACH_CONSOLE=

REM ---- locate node.exe ----
REM 1) project-local node
if exist "%ROOT%\node\node.exe" set "NODE_EXE=%ROOT%\node\node.exe"

REM 2) NODE_HOME (optional: set NODE_HOME=C:\nodejs before running, if node sits in a fixed folder)
if not defined NODE_EXE if defined NODE_HOME (
    if exist "%NODE_HOME%\versions\current" set /p NODE_VER=<"%NODE_HOME%\versions\current"
    if defined NODE_VER if exist "%NODE_HOME%\versions\!NODE_VER!\node.exe" set "NODE_EXE=%NODE_HOME%\versions\!NODE_VER!\node.exe"
    if not defined NODE_EXE if exist "%NODE_HOME%\node.exe" set "NODE_EXE=%NODE_HOME%\node.exe"
    if not defined NODE_EXE if exist "%NODE_HOME%\versions" (
        for /d %%D in ("%NODE_HOME%\versions\*") do (
            if not defined NODE_EXE if exist "%%~D\node.exe" set "NODE_EXE=%%~D\node.exe"
        )
    )
)

REM 3) node on PATH
if not defined NODE_EXE (
    for %%P in (node.exe) do if not defined NODE_EXE set "NODE_EXE=%%~$PATH:P"
)

if not defined NODE_EXE (
    echo.
    echo   [ERROR] Node.js not found. Install Node.js 24.15 or later, or set NODE_HOME first.
    echo.
    pause
    exit /b 1
)

if not exist "%LAUNCH_JS%" (
    echo.
    echo   [ERROR] launch.js not found:
    echo   %LAUNCH_JS%
    echo.
    pause
    exit /b 1
)

if not exist "%APP_DIR%\node_modules\electron" (
    echo.
    echo   [ERROR] Electron not installed.
    echo   Run:  cd /d "%APP_DIR%" ^&^& npm ci ^&^& npm run setup:electron
    echo.
    pause
    exit /b 1
)

echo.
echo   ============================================
echo     TianShu-1  Distribution Power Flow Agent
echo   ============================================
echo.
echo   ROOT     = %ROOT%
echo   APP_DIR  = %APP_DIR%
echo   NODE_EXE = %NODE_EXE%
echo.
echo   App window opens in a few seconds.
echo   Engine loads in a private process when a task starts.
echo   Please DO NOT close this window.
echo.
echo   ============================================
echo.

cd /d "%APP_DIR%"
"%NODE_EXE%" "%LAUNCH_JS%"
set EL_ERROR=%errorlevel%

if not "%EL_ERROR%"=="0" (
    echo.
    echo   [ERROR] Exit code: %EL_ERROR%
    echo.
    echo   Logs:
    echo     %APPDATA%\tianshu-1\logs\tianshu-main.log
    echo     %APPDATA%\tianshu-1\logs\tianshu-backend.log
    echo.
    pause
)
