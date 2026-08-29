@echo off
cd /d %~dp0
python monitor.py monitor --config config.json
pause
