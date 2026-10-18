@echo off
rem One click for Windows: downloads Fedora IoT, writes the SD card, installs Shelly-Pi on the Pi.
rem Options are passed on, e.g.  install.cmd -SkipSd   or   install.cmd -PiHost 192.168.1.50
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" %*
echo.
pause
