@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "APP_DIR=%SCRIPT_DIR%"
set "INSTALL_DIR=%SCRIPT_DIR%"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

if exist "%SCRIPT_DIR%Update-StocksTool.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Update-StocksTool.ps1" -InstallDir "%INSTALL_DIR%" -Quiet
)

if exist "%SCRIPT_DIR%StocksTradingAssistant.exe" (
  start "" "%SCRIPT_DIR%StocksTradingAssistant.exe"
  exit /b 0
)

if exist "%SCRIPT_DIR%app\StocksTradingAssistant.exe" (
  start "" "%SCRIPT_DIR%app\StocksTradingAssistant.exe"
  exit /b 0
)

echo.
echo StocksTradingAssistant.exe was not found.
echo Tried:
echo   "%SCRIPT_DIR%StocksTradingAssistant.exe"
echo   "%SCRIPT_DIR%app\StocksTradingAssistant.exe"
pause
exit /b 1
