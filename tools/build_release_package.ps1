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
$AppPayloadRoot = Join-Path $PackageRoot "app"
$PyInstallerWork = Join-Path $TempRoot "pyinstaller_work"
$PyInstallerDist = Join-Path $TempRoot "pyinstaller_dist"
$DistRoot = Join-Path $Root $OutputDir
$ZipPath = Join-Path $DistRoot "$PackageName.zip"
$ManagedConfigPatterns = @(
    "hit_rate_calibration.default.json",
    "live_positions.example.csv",
    "watchlist.*.csv",
    "weak_catalysts.json",
    "xueqiu_cookie.example.txt"
)
$UserOwnedConfigFiles = @(
    "broker_account_snapshot.json",
    "live_positions.csv",
    "ui_settings.json",
    "xueqiu_cookie.txt"
)

Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
New-Item -ItemType Directory -Force -Path $AppPayloadRoot | Out-Null
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null

function Get-RelativePathText {
    param(
        [string]$Base,
        [string]$Path
    )
    $baseFull = (Resolve-Path $Base).Path.TrimEnd("\") + "\"
    $pathFull = (Resolve-Path $Path).Path
    $baseUri = New-Object System.Uri($baseFull)
    $pathUri = New-Object System.Uri($pathFull)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($pathUri).ToString()).Replace("/", "\")
}

function New-ManifestEntry {
    param(
        [string]$SourceBase,
        [string]$SourcePath,
        [string]$TargetPath
    )
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $SourcePath
    $file = Get-Item -LiteralPath $SourcePath
    return [ordered]@{
        source = (Get-RelativePathText -Base $PackageRoot -Path $SourcePath)
        target = $TargetPath.Replace("/", "\")
        sha256 = $hash.Hash.ToLowerInvariant()
        size = $file.Length
    }
}

function Test-ManagedConfigFile {
    param([string]$RelativePath)
    $name = Split-Path -Leaf $RelativePath
    if ($UserOwnedConfigFiles -contains $name) {
        return $false
    }
    foreach ($pattern in $ManagedConfigPatterns) {
        if ($name -like $pattern) {
            return $true
        }
    }
    return $false
}

function Copy-ManagedConfigFiles {
    param(
        [string]$SourceDir,
        [string]$DestinationDir
    )
    New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
    foreach ($file in Get-ChildItem -LiteralPath $SourceDir -Recurse -File) {
        $relative = Get-RelativePathText -Base $SourceDir -Path $file.FullName
        if (-not (Test-ManagedConfigFile $relative)) {
            continue
        }
        $target = Join-Path $DestinationDir $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -Force -LiteralPath $file.FullName -Destination $target
    }
}

Copy-Item -Force (Join-Path $Root "installer\Install-StocksTool.ps1") (Join-Path $PackageRoot "Install-StocksTool.ps1")
Copy-Item -Force (Join-Path $Root "installer\Update-StocksTool.ps1") (Join-Path $PackageRoot "Update-StocksTool.ps1")
Copy-Item -Force (Join-Path $Root "installer\Start-TradingAssistant.bat") (Join-Path $PackageRoot "Start-TradingAssistant.bat")
Copy-Item -Force (Join-Path $Root "VERSION") (Join-Path $PackageRoot "VERSION")
Copy-ManagedConfigFiles -SourceDir (Join-Path $Root "config") -DestinationDir (Join-Path $PackageRoot "config")

& python -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE"
}

& python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
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

Copy-Item -Recurse -Force (Join-Path $PyInstallerDist "StocksTradingAssistant\*") $AppPayloadRoot

$manifest = [ordered]@{
    name = "Stocks Trading Assistant"
    version = $Version
    repository = "Sulfoxide319/Stocks"
    generated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    executable = "StocksTradingAssistant.exe"
    package_layout = "onedir"
    app_dir = "app"
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
creates a desktop shortcut. Python is not required for the packaged app.

## Start

After installation, use the desktop shortcut or run:

```text
StocksTradingAssistant.exe
```

You can also run `Start-TradingAssistant.bat` directly from the extracted zip
folder. It checks for updates and launches the packaged app.

The start script checks GitHub Releases for updates before launching the app.
"@
$readme | Set-Content -Encoding UTF8 (Join-Path $PackageRoot "README_INSTALL.md")

$files = @()
foreach ($file in Get-ChildItem -LiteralPath $AppPayloadRoot -Recurse -File) {
    $target = Get-RelativePathText -Base $AppPayloadRoot -Path $file.FullName
    $files += New-ManifestEntry -SourceBase $PackageRoot -SourcePath $file.FullName -TargetPath $target
}
foreach ($name in @("Update-StocksTool.ps1", "Start-TradingAssistant.bat", "VERSION", "release.json")) {
    $source = Join-Path $PackageRoot $name
    if (Test-Path $source) {
        $files += New-ManifestEntry -SourceBase $PackageRoot -SourcePath $source -TargetPath $name
    }
}
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $PackageRoot "config") -Recurse -File) {
    $target = Get-RelativePathText -Base $PackageRoot -Path $file.FullName
    $files += New-ManifestEntry -SourceBase $PackageRoot -SourcePath $file.FullName -TargetPath $target
}
$updateManifest = [ordered]@{
    schema = 1
    name = "Stocks Trading Assistant"
    version = $Version
    repository = "Sulfoxide319/Stocks"
    layout = "onedir"
    executable = "StocksTradingAssistant.exe"
    generated_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    files = $files
}
$updateManifest | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $PackageRoot "update_manifest.json")

Remove-Item -Force $ZipPath -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $ZipPath -Force

$auditScript = Join-Path $Root "tools\audit_release_package.py"
if (Test-Path $auditScript) {
    & python $auditScript $ZipPath --expected-version $Version
    if ($LASTEXITCODE -ne 0) {
        throw "Release package audit failed with exit code $LASTEXITCODE"
    }
}

Write-Host "Built $ZipPath"
exit 0
