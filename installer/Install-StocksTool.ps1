param(
    [string]$InstallDir = "$env:LOCALAPPDATA\StocksTradingAssistant",
    [string]$PackageRoot = "",
    [switch]$SkipDependencyInstall,
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python was not found on PATH. Install Python 3.10+ and run this installer again."
    }
    return $python.Source
}

function Copy-DirectoryContent {
    param(
        [string]$Source,
        [string]$Destination
    )
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $excludeDirs = @(".git", ".github", ".xueqiu-edge-profile", "__pycache__", "node_modules", "output", "outputs", "dist", "release_tmp")
    $excludeFiles = @("*.pyc", "*.pyo", "*.log")
    $args = @($Source, $Destination, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    foreach ($dir in $excludeDirs) {
        $args += @("/XD", (Join-Path $Source $dir))
    }
    foreach ($file in $excludeFiles) {
        $args += @("/XF", $file)
    }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -gt 7) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }
}

if (-not $PackageRoot) {
    $PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$PackageRoot = (Resolve-Path $PackageRoot).Path
$AppSource = Join-Path $PackageRoot "app"
if (-not (Test-Path $AppSource)) {
    $AppSource = $PackageRoot
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-DirectoryContent -Source $AppSource -Destination $InstallDir

foreach ($file in @("StocksTradingAssistant.exe", "Update-StocksTool.ps1", "Start-TradingAssistant.bat", "VERSION", "release.json")) {
    $source = Join-Path $PackageRoot $file
    if (Test-Path $source) {
        Copy-Item -Force $source (Join-Path $InstallDir $file)
    }
}

if (-not $SkipDependencyInstall) {
    $exePath = Join-Path $InstallDir "StocksTradingAssistant.exe"
    if (-not (Test-Path $exePath)) {
        $pythonExe = Resolve-Python
        $requirements = Join-Path $InstallDir "requirements.txt"
        if (Test-Path $requirements) {
            & $pythonExe -m pip install -r $requirements
            if ($LASTEXITCODE -ne 0) {
                throw "Dependency installation failed."
            }
        }
    }
}

if (-not $NoShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ($desktop) {
        $shortcutPath = Join-Path $desktop "Stocks Trading Assistant.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $exePath = Join-Path $InstallDir "StocksTradingAssistant.exe"
        if (Test-Path $exePath) {
            $shortcut.TargetPath = $exePath
        } else {
            $shortcut.TargetPath = Join-Path $InstallDir "Start-TradingAssistant.bat"
        }
        $shortcut.WorkingDirectory = $InstallDir
        $shortcut.Description = "Stocks Trading Assistant"
        $shortcut.Save()
    }
}

Write-Host "Installed Stocks Trading Assistant to $InstallDir"
$installedExe = Join-Path $InstallDir "StocksTradingAssistant.exe"
if (Test-Path $installedExe) {
    Write-Host "Start with: $installedExe"
} else {
    Write-Host "Start with: $(Join-Path $InstallDir 'Start-TradingAssistant.bat')"
}
exit 0
