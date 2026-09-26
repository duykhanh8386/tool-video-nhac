@echo off
setlocal
cd /d "%~dp0visual_loop_studio"
if not exist ".venv\Scripts\python.exe" (
  echo Environment not installed. Run install.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo PySide6 is not installed completely. Run install.bat again.
  pause
  exit /b 1
)
.venv\Scripts\python.exe main.py
