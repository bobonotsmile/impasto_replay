@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Impasto Replay V3...
py -3.11 server.py --open
pause
