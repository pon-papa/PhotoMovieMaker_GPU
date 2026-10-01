@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
rem ============================================================
rem  PhotoMovieMaker GPU - setup
rem  このフォルダーの中に .venv を作り、必要なパッケージをその中だけに入れます。
rem  PC 全体の Python には何も入れません。管理者権限は要りません。
rem  2 回実行しても大丈夫です（.venv があれば再利用します）。
rem ============================================================
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

echo.
echo === PhotoMovieMaker GPU セットアップ / setup ===
echo.

if not exist "%APP_DIR%PhotoMovieMaker_GPU.py" goto :incomplete
if not exist "%APP_DIR%requirements.txt" goto :incomplete
if not exist "%APP_DIR%app_doctor.py" goto :incomplete
set "PYTHONUTF8=1"

set "VENV_PY=%APP_DIR%.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :check_venv

rem --- Python 3.13 / 3.12 / 3.11 を探す（py ランチャーを優先）
set "PY_CMD="
for %%V in (3.13 3.12 3.11) do call :try_py %%V
if defined PY_CMD goto :make_venv
python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"
if defined PY_CMD goto :make_venv
goto :no_python

:make_venv
echo [1/4] 仮想環境 .venv を作っています / creating .venv ...
%PY_CMD% -m venv "%APP_DIR%.venv"
if errorlevel 1 goto :venv_failed
if not exist "%VENV_PY%" goto :venv_failed
goto :install

:check_venv
echo [1/4] 既にある .venv を使います / reusing existing .venv
"%VENV_PY%" -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1
if errorlevel 1 goto :venv_broken

:install
echo [2/4] pip を更新しています / upgrading pip ...
"%VENV_PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :pip_failed
echo [3/4] 必要なパッケージを入れています / installing packages ...
"%VENV_PY%" -m pip install --disable-pip-version-check -r "%APP_DIR%requirements.txt"
if errorlevel 1 goto :pip_failed
echo [4/4] 確認しています / checking ...
"%VENV_PY%" "%APP_DIR%app_doctor.py" --brief
if errorlevel 1 goto :check_failed

echo.
echo セットアップが終わりました。次は run.bat をダブルクリックしてください。
echo Setup complete. Next, double-click run.bat
echo.
goto :done_ok

:try_py
if defined PY_CMD exit /b 0
py -%1 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -%1"
exit /b 0

:incomplete
echo [エラー] アプリのファイルが足りません。
echo         配布 ZIP をすべて展開してから、展開したフォルダーの中の setup.bat を実行してください。
echo [ERROR] Application files are missing. Extract the whole ZIP first.
goto :done_ng

:no_python
echo [エラー] Python 3.11 から 3.13 が見つかりません。
echo         https://www.python.org/downloads/windows/ から Python 3.13 などをインストールし、
echo         もう一度 setup.bat を実行してください。
echo [ERROR] Python 3.11 - 3.13 was not found. Install it, then run setup.bat again.
goto :done_ng

:venv_failed
echo [エラー] 仮想環境 .venv を作れませんでした。
echo         このフォルダーに書き込めるか確かめてください。
echo         書き込みに管理者権限が要る場所ではなく、ドキュメントなどへ展開し直してください。
echo [ERROR] Could not create .venv. Extract the app to a folder you can write to.
goto :done_ng

:venv_broken
echo [エラー] 既にある .venv が使えません（Python を入れ替えた・フォルダーの中身が壊れた など）。
echo         このフォルダーの中の .venv フォルダーを削除してから、もう一度 setup.bat を実行してください。
echo [ERROR] The existing .venv is not usable. Delete the .venv folder and run setup.bat again.
goto :done_ng

:pip_failed
echo [エラー] パッケージを入れられませんでした。
echo         インターネットに接続できるか確かめて、もう一度 setup.bat を実行してください。
echo         （setup のときだけ、PyPI からパッケージを取得します）
echo [ERROR] Package installation failed. Check your internet connection and run setup.bat again.
goto :done_ng

:check_failed
echo [エラー] 確認で問題が見つかりました。上の表示を見てください。
echo         詳しくは diagnose.bat を実行してください。
echo [ERROR] The check found a problem. See the messages above, or run diagnose.bat
goto :done_ng

:done_ok
if not defined PMM_NO_PAUSE pause
endlocal
exit /b 0

:done_ng
echo.
if not defined PMM_NO_PAUSE pause
endlocal
exit /b 1
