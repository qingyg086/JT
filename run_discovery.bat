@echo off
cd /d %~dp0
python monitor.py discover --config config.json
pause
