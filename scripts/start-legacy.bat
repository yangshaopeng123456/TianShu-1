@echo off
REM Entry point at project level. Delegates to the desktop-shell launcher.
call "%~dp0..\tianshu_ui\start-tianshu.bat" %*
