@echo off
cd /d "%~dp0"
pixi run python run-ashlar.py --gui
if %ERRORLEVEL% neq 0 pause
