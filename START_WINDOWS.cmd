@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 experiment.py --interactive
pause
