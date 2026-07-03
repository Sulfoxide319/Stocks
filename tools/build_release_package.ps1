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
$AppRoot = Join-Path $PackageRoot "app"
$DistRoot = Join-Path $Root $OutputDir
$ZipPath = Join-Path $DistRoot "$PackageName.zip"

Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $AppRoot | Out-Null
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null

$excludeDirs = @(".git", ".github", ".xueqiu-edge-profile", "__pycache__", "node_modules", "output", "outputs", "dist", "release_tmp")
$excludeFiles = @("*.pyc", "*.pyo", "*.log")
$robocopyArgs = @($Root, $AppRoot, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
foreach ($dir in $excludeDirs) {
    $robocopyArgs += @("/XD", (Join-Path $Root $dir))
}
foreach ($file in $excludeFiles) {
    $robocopyArgs += @("/XF", $file)
}
& robocopy @robocopyArgs | Out-Null
if ($LASTEXITCODE -gt 7) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

Copy-Item -Force (Join-Path $Root "installer\Install-StocksTool.ps1") (Join-Path $PackageRoot "Install-StocksTool.ps1")
Copy-Item -Force (Join-Path $Root "installer\Update-StocksTool.ps1") (Join-Path $PackageRoot "Update-StocksTool.ps1")
Copy-Item -Force (Join-Path $Root "installer\Start-TradingAssistant.bat") (Join-Path $PackageRoot "Start-TradingAssistant.bat")
Copy-Item -Force (Join-Path $Root "VERSION") (Join-Path $PackageRoot "VERSION")

$manifest = [ordered]@{
    name = "Stocks Trading Assistant"
    version = $Version
    repository = "Sulfoxide319/Stocks"
    generated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
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

The installer copies the app to `%LOCALAPPDATA%\StocksTradingAssistant`,
installs Python dependencies from `requirements.txt`, and creates a desktop
shortcut.

## Start

After installation, use the desktop shortcut or run:

```text
Start-TradingAssistant.bat
```

You can also run `Start-TradingAssistant.bat` directly from the extracted zip
folder. In that portable mode it launches the app from the bundled `app`
directory.

The start script checks GitHub Releases for updates before launching the app.
"@
$readme | Set-Content -Encoding UTF8 (Join-Path $PackageRoot "README_INSTALL.md")

Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -Force

Write-Host "Built $ZipPath"
exit 0
