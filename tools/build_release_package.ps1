param(
    [string]$Version = "",
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Version) {
    $Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
}

$PackageName = "StocksTradingAssistant-v$Version"
$TempRoot = Join-Path $Root "release_tmp"
$PackageRoot = Join-Path $TempRoot $PackageName
$PyInstallerWork = Join-Path $TempRoot "pyinstaller_work"
$PyInstallerDist = Join-Path $TempRoot "pyinstaller_dist"
$DistRoot = Join-Path $Root $OutputDir
$ZipPath = Join-Path $DistRoot "$PackageName.zip"

Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null

Copy-Item -Force (Join-Path $Root "installer\Install-StocksTool.ps1") (Join-Path $PackageRoot "Install-StocksTool.ps1")
Copy-Item -Force (Join-Path $Root "installer\Update-StocksTool.ps1") (Join-Path $PackageRoot "Update-StocksTool.ps1")
Copy-Item -Force (Join-Path $Root "installer\Start-TradingAssistant.bat") (Join-Path $PackageRoot "Start-TradingAssistant.bat")
Copy-Item -Force (Join-Path $Root "VERSION") (Join-Path $PackageRoot "VERSION")

& python -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE"
}

& python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name StocksTradingAssistant `
    --hidden-import short_term_live_monitor `
    --exclude-module torch `
    --exclude-module torchvision `
    --exclude-module torchaudio `
    --exclude-module tensorflow `
    --exclude-module transformers `
    --exclude-module scipy `
    --exclude-module matplotlib `
    --exclude-module numba `
    --exclude-module llvmlite `
    --exclude-module triton `
    --exclude-module onnxruntime `
    --exclude-module pytest `
    --exclude-module altair `
    --distpath $PyInstallerDist `
    --workpath $PyInstallerWork `
    --specpath $TempRoot `
    (Join-Path $Root "desktop_app.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Copy-Item -Force (Join-Path $PyInstallerDist "StocksTradingAssistant.exe") (Join-Path $PackageRoot "StocksTradingAssistant.exe")

$manifest = [ordered]@{
    name = "Stocks Trading Assistant"
    version = $Version
    repository = "Sulfoxide319/Stocks"
    generated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    executable = "StocksTradingAssistant.exe"
    start_script = "Start-TradingAssistant.bat"
    installer = "Install-StocksTool.ps1"
    updater = "Update-StocksTool.ps1"
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $PackageRoot "release.json")

$readme = @"
# Stocks Trading Assistant $Version

## Install

Right click `Install-StocksTool.ps1`, choose **Run with PowerShell**, or run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-StocksTool.ps1
```

The installer copies the app to `%LOCALAPPDATA%\StocksTradingAssistant` and
creates a desktop shortcut. Python is not required for the packaged EXE.

## Start

After installation, use the desktop shortcut or run:

```text
StocksTradingAssistant.exe
```

You can also run `Start-TradingAssistant.bat` directly from the extracted zip
folder. It checks for updates and launches the packaged EXE.

The start script checks GitHub Releases for updates before launching the app.
"@
$readme | Set-Content -Encoding UTF8 (Join-Path $PackageRoot "README_INSTALL.md")

Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -Force

Write-Host "Built $ZipPath"
exit 0
