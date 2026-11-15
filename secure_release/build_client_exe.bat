@echo off
setlocal
cd /d "%~dp0"
py -m pip install pyinstaller
pyinstaller --onefile --noconsole client.py

echo.
echo EXE siap di: %~dp0dist\client.exe
pause
