@echo off
cd /d "%~dp0"
if not exist "local-server\.venv\Scripts\python.exe" (
  echo Run the installation steps in INSTALL.md first.
  pause
  exit /b 1
)
"local-server\.venv\Scripts\python.exe" "local-server\scripts\run_server.py"
pause
