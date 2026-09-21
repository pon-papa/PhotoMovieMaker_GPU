@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === PhotoMovieMaker GPU setup ===
echo.
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
echo.
echo Setup complete.
echo Next, double-click run.bat
echo.
pause
