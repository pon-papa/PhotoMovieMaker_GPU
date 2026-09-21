@echo off
chcp 65001 >nul
cd /d "%~dp0"
py PhotoMovieMaker_GPU.py
if errorlevel 1 pause
