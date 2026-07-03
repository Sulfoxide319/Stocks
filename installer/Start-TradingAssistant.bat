@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "APP_DIR=%SCRIPT_DIR%"
set "INSTALL_DIR=%SCRIPT_DIR%"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

if not exist "%APP_DIR%trading_assistant_app.py" (
  if exist "%SCRIPT_DIR%app\trading_assistant_app.py" (
    set "APP_DIR=%SCRIPT_DIR%app\"
  )
)

if exist "%SCRIPT_DIR%Update-StocksTool.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Update-StocksTool.ps1" -InstallDir "%INSTALL_DIR%" -Quiet
)

if exist "%SCRIPT_DIR%StocksTradingAssistant.exe" (
  start "" "%SCRIPT_DIR%StocksTradingAssistant.exe"
  exit /b 0
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
