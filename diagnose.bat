@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
rem PhotoMovieMaker GPU - 診断。この PC でアプリが動く条件がそろっているかを表示します。
rem 何も変更しません。結果にユーザー名やフォルダーの場所は出ません（不具合の報告に貼れます）。
set "APP_DIR=%~dp0"
set "VENV_PY=%APP_DIR%.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
if not exist "%APP_DIR%app_doctor.py" goto :incomplete
cd /d "%APP_DIR%"
echo.
if exist "%VENV_PY%" goto :with_venv

echo .venv がまだありません。PC にある Python で、分かる範囲を診断します。
echo.
py -3 "%APP_DIR%app_doctor.py"
if not errorlevel 9009 goto :finish
python "%APP_DIR%app_doctor.py"
if not errorlevel 9009 goto :finish
echo [エラー] Python が見つかりません。
echo         Python 3.11 から 3.13 をインストールし、setup.bat を実行してください。
echo [ERROR] Python was not found. Install Python 3.11 - 3.13, then run setup.bat
goto :finish

:with_venv
"%VENV_PY%" "%APP_DIR%app_doctor.py"
goto :finish

:incomplete
echo [エラー] アプリのファイルが足りません。配布 ZIP をすべて展開してください。
echo [ERROR] Application files are missing. Extract the whole ZIP first.

:finish
echo.
if not defined PMM_NO_PAUSE pause
endlocal
exit /b 0
