@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"

rem =========================================
rem 1. Stop stale app on busy ports
rem =========================================
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :5000 2^>nul') do (
    echo Stopping PID %%p on port 5000
    taskkill /PID %%p /F 2>nul
)

for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8501 2^>nul') do (
    echo Stopping PID %%p on port 8501
    taskkill /PID %%p /F 2>nul
)

rem =========================================
rem 2. Check required files
rem =========================================
if not exist "%ROOT%\server.py" (
    echo ERROR: server.py not found.
    pause
    exit /b 1
)

if not exist "%ROOT%\dashboard.py" (
    echo ERROR: dashboard.py not found.
    pause
    exit /b 1
)

rem =========================================
rem 3. Start API server
rem =========================================
start "Hospital API" /D "%ROOT%" cmd /k "py server.py"

rem =========================================
rem 4. Start dashboard
rem =========================================
start "DKT Monitoring Dashboard" /D "%ROOT%" cmd /k "py -m streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true"

echo.
echo API ready: http://127.0.0.1:5000/api/health
echo Dashboard ready: http://127.0.0.1:8501/
echo.
echo Script selesai. Jika port masih terpakai, run: netstat -ano ^| findstr :5000
pause > nul
