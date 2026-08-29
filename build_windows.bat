@echo off
cd /d %~dp0
set PLAYWRIGHT_BROWSERS_PATH=0
python -m pip install -r requirements.txt pyinstaller
python -m playwright install chromium
pyinstaller --onedir --name jt-seat-monitor-ui --collect-all playwright --add-data "config.example.json;." web_ui.py
copy config.example.json dist\jt-seat-monitor-ui\config.json
echo.
echo UI version generated at dist\jt-seat-monitor-ui\jt-seat-monitor-ui.exe
echo Send the whole dist\jt-seat-monitor-ui folder to the customer.
pause
