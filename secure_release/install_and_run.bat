@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ========================================
echo Secure Release Installer
echo ========================================

echo [1/4] Checking Python...
where py >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    pause
    exit /b 1
)

echo [2/4] Installing Python dependencies...
py -m pip install --upgrade pip >nul
py -m pip install -r requirements.txt

if not exist "%ROOT%\.env" (
    echo [3/4] Creating .env from template...
    copy "%ROOT%\.env.example" "%ROOT%\.env" >nul
    echo.
    echo IMPORTANT: Edit %ROOT%\.env before running the app.
    echo Replace the placeholder values for CLIENT_SECRET_TOKEN, ROUTER_USERNAME, and ROUTER_PASSWORD.
    echo.
    pause
    exit /b 0
)

echo [3/4] Starting services...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :5000 2^>nul') do (
    echo Stopping PID %%p on port 5000
    taskkill /PID %%p /F 2>nul
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8501 2^>nul') do (
    echo Stopping PID %%p on port 8501
    taskkill /PID %%p /F 2>nul
)

start "Hospital API" /D "%ROOT%" cmd /k "py server.py"
start "Dashboard" /D "%ROOT%" cmd /k "py -m streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true"

echo [4/4] Service startup started.

echo.
echo API: http://127.0.0.1:5000/api/health
echo Dashboard: http://127.0.0.1:8501/

echo.
echo Done.
pause >nul
