@echo off
title Install VideoLearn
rem Double-click to install. Extra options are passed through, e.g.: "Install VideoLearn.bat" -NoGpu
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
