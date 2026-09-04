@echo off
cd /d %~dp0
set PLAYWRIGHT_BROWSERS_PATH=0
python -m venv .venv-build
.venv-build\Scripts\python -m pip install --upgrade pip
.venv-build\Scripts\python -m pip install --no-cache-dir -r requirements.txt pyinstaller
.venv-build\Scripts\python -m playwright install chromium
.venv-build\Scripts\pyinstaller --clean --onedir --name jt-seat-monitor-ui --collect-data playwright --collect-binaries playwright --add-data "config.example.json;." web_ui.py
copy config.example.json dist\jt-seat-monitor-ui\config.json
echo.
echo UI version generated at dist\jt-seat-monitor-ui\jt-seat-monitor-ui.exe
echo Send the whole dist\jt-seat-monitor-ui folder to the customer.
pause
