@echo off
setlocal
cd /d "%~dp0visual_loop_studio"
where python >nul 2>nul || (echo Python was not found. Install Python 3.11+ first. & pause & exit /b 1)
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installation failed. Make sure the system drive has enough temporary space, then run this file again.
  pause
  exit /b 1
)
echo.
echo Installation complete. Run run.bat.
pause
