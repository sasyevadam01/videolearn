@echo off
title Install VideoLearn 4Claude
rem Double-click to install. Extra options are passed through, e.g.: "Install VideoLearn 4Claude.bat" -NoGpu
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
