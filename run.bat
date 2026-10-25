@echo off
cd /d %~dp0
echo [1/4] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install Python 3.11 or 3.12 from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
python --version
if not exist venv python -m venv venv
call venv\Scripts\activate
echo [2/4] Installing web server...
pip install -q fastapi "uvicorn[standard]" opencv-python numpy
if errorlevel 1 (
  echo Install failed. Check internet connection / Python version.
  pause
  exit /b 1
)
echo [3/4] Installing AI (ultralytics, large download, first time only)...
pip install -q ultralytics
if errorlevel 1 echo WARNING: ultralytics install failed. Web will open but AI counting will not work. Use Python 3.11 or 3.12.
netsh advfirewall firewall add rule name="ParkingAI" dir=in action=allow protocol=TCP localport=8000 >nul 2>nul
echo [4/4] Starting server. Open http://localhost:8000
echo.
uvicorn server:app --host 0.0.0.0 --port 8000
echo.
echo Server stopped. Read the error above.
pause
