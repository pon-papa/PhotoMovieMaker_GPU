@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
rem PhotoMovieMaker GPU - 起動。setup.bat が作った .venv の Python だけを使います。
set "APP_DIR=%~dp0"
set "VENV_PY=%APP_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :no_venv
if not exist "%APP_DIR%PhotoMovieMaker_GPU.py" goto :incomplete
cd /d "%APP_DIR%"
rem OpenCV の情報・警告の行をコンソールに出さない（エラーは出ます）
set "OPENCV_LOG_LEVEL=ERROR"
"%VENV_PY%" "%APP_DIR%PhotoMovieMaker_GPU.py"
if errorlevel 1 goto :failed
endlocal
exit /b 0

:no_venv
echo.
echo まず setup.bat を実行してください。
echo （このフォルダーの中に .venv がまだありません）
echo Please run setup.bat first.
goto :done_ng

:incomplete
echo.
echo [エラー] アプリのファイルが足りません。配布 ZIP をすべて展開してください。
echo [ERROR] Application files are missing. Extract the whole ZIP first.
goto :done_ng

:failed
echo.
echo [エラー] アプリがエラーで終了しました。上の表示を確かめてください。
echo         原因が分からないときは diagnose.bat を実行し、その結果を添えて報告してください。
echo [ERROR] The app exited with an error. Run diagnose.bat and include its output when reporting.
goto :done_ng

:done_ng
echo.
if not defined PMM_NO_PAUSE pause
endlocal
exit /b 1
