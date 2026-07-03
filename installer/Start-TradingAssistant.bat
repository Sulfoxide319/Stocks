@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-StocksTool.ps1" -Quiet

python trading_assistant_app.py
if errorlevel 1 (
  echo.
  echo Trading assistant exited with an error.
  pause
)
