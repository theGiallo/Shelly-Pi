@echo off
rem Double-click to install Shelly-Pi on a Raspberry Pi. Extra options are passed on, e.g.  install.cmd -PiHost 192.168.1.50
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
echo.
pause
