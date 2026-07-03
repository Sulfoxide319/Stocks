param(
    [string]$InstallDir = "$env:LOCALAPPDATA\StocksTradingAssistant",
    [string]$Repository = "Sulfoxide319/Stocks",
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$InstallDir = $InstallDir.Trim().Trim('"').TrimEnd("\")

function Write-UpdateLog {
    param([string]$Message)
    if (-not $Quiet) {
        Write-Host $Message
    }
}

function Get-InstalledVersion {
    $versionPath = Join-Path $InstallDir "VERSION"
    if (Test-Path $versionPath) {
        return (Get-Content $versionPath -Raw).Trim()
    }
    return "0.0.0"
}

function Convert-ToVersion {
    param([string]$Value)
    $clean = $Value.Trim().TrimStart("v")
    try {
        return [version]$clean
    } catch {
        return [version]"0.0.0"
    }
}

function Invoke-GitHubGet {
    param([string]$Uri)
    $headers = @{
        "User-Agent" = "StocksTradingAssistant-Updater"
        "Accept" = "application/vnd.github+json"
    }
    return Invoke-RestMethod -Uri $Uri -Headers $headers
}

$currentText = Get-InstalledVersion
$current = Convert-ToVersion $currentText
$releaseUri = "https://api.github.com/repos/$Repository/releases/latest"

try {
    $release = Invoke-GitHubGet $releaseUri
} catch {
    Write-UpdateLog "Update check failed: $($_.Exception.Message)"
    exit 0
}

$latestText = [string]$release.tag_name
$latest = Convert-ToVersion $latestText
if (-not $Force -and $latest -le $current) {
    Write-UpdateLog "Already up to date: $currentText"
    exit 0
}

$asset = $release.assets | Where-Object { $_.name -like "StocksTradingAssistant-v*.zip" } | Select-Object -First 1
if (-not $asset) {
    Write-UpdateLog "Latest release has no StocksTradingAssistant zip asset."
    exit 0
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("stocks-tool-update-" + [Guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tempRoot $asset.name
$extractDir = Join-Path $tempRoot "package"
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

try {
    Write-UpdateLog "Downloading $($asset.name)..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -Headers @{ "User-Agent" = "StocksTradingAssistant-Updater" }
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

    $installer = Get-ChildItem -Path $extractDir -Filter "Install-StocksTool.ps1" -Recurse | Select-Object -First 1
    if (-not $installer) {
        throw "Installer was not found inside release package."
    }

    Write-UpdateLog "Updating from $currentText to $latestText..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer.FullName -InstallDir $InstallDir -SkipDependencyInstall -NoShortcut
    if ($LASTEXITCODE -ne 0) {
        throw "Installer returned exit code $LASTEXITCODE"
    }
    Write-UpdateLog "Updated to $latestText"
} catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
}
