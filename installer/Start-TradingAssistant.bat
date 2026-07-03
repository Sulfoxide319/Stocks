@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "APP_DIR=%SCRIPT_DIR%"

if not exist "%APP_DIR%trading_assistant_app.py" (
  if exist "%SCRIPT_DIR%app\trading_assistant_app.py" (
    set "APP_DIR=%SCRIPT_DIR%app\"
  )
)

if exist "%SCRIPT_DIR%Update-StocksTool.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Update-StocksTool.ps1" -InstallDir "%SCRIPT_DIR%" -Quiet
)

if exist "%SCRIPT_DIR%trading_assistant_app.py" (
  set "APP_DIR=%SCRIPT_DIR%"
)

cd /d "%APP_DIR%"
python trading_assistant_app.py
if errorlevel 1 (
  echo.
  echo Tried app directory: "%APP_DIR%"
  echo Trading assistant exited with an error.
  pause
)
