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
    $excludeFiles = @("*.pyc", "*.pyo", "*.log", "broker_account_snapshot.json", "live_positions.csv", "ui_settings.json", "xueqiu_cookie.txt")
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

function Test-UserOwnedPath {
    param([string]$RelativePath)
    $normalized = $RelativePath.Replace("/", "\").TrimStart("\").ToLowerInvariant()
    return $normalized -in @(
        "config\broker_account_snapshot.json",
        "config\live_positions.csv",
        "config\ui_settings.json",
        "config\xueqiu_cookie.txt"
    )
}

function Get-FileSha256 {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return ""
    }
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Copy-ChangedManagedFiles {
    param(
        [string]$PackageRoot,
        [string]$InstallDir,
        [object]$Manifest
    )
    $copied = 0
    $targets = @{}
    foreach ($file in $Manifest.files) {
        $relativeTarget = [string]$file.target
        $targets[$relativeTarget] = $true
        if (Test-UserOwnedPath $relativeTarget) {
            continue
        }
        $source = Join-Path $PackageRoot ([string]$file.source)
        $target = Join-Path $InstallDir $relativeTarget
        if (-not (Test-Path $source)) {
            throw "Package file was not found: $source"
        }
        $targetHash = Get-FileSha256 $target
        if ($targetHash -ne [string]$file.sha256) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -Force -LiteralPath $source -Destination $target
            $copied += 1
        }
    }

    $previousManifestPath = Join-Path $InstallDir ".install_manifest.json"
    if (Test-Path $previousManifestPath) {
        try {
            $previous = Get-Content $previousManifestPath -Raw | ConvertFrom-Json
            foreach ($oldFile in $previous.files) {
                $oldTarget = [string]$oldFile.target
                if (-not $targets.ContainsKey($oldTarget)) {
                    if (Test-UserOwnedPath $oldTarget) {
                        continue
                    }
                    $oldPath = Join-Path $InstallDir $oldTarget
                    if (Test-Path $oldPath) {
                        Remove-Item -Force -LiteralPath $oldPath
                    }
                }
            }
        } catch {
            # A bad old manifest should not block installation.
        }
    }

    Copy-Item -Force -LiteralPath (Join-Path $PackageRoot "update_manifest.json") -Destination $previousManifestPath
    return $copied
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
$manifestPath = Join-Path $PackageRoot "update_manifest.json"
if (Test-Path $manifestPath) {
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    $copiedCount = Copy-ChangedManagedFiles -PackageRoot $PackageRoot -InstallDir $InstallDir -Manifest $manifest
    Write-Host "Applied $copiedCount changed file(s)."
} else {
    Copy-DirectoryContent -Source $AppSource -Destination $InstallDir

    foreach ($file in @("StocksTradingAssistant.exe", "Update-StocksTool.ps1", "Start-TradingAssistant.bat", "VERSION", "release.json")) {
        $source = Join-Path $PackageRoot $file
        if (Test-Path $source) {
            Copy-Item -Force $source (Join-Path $InstallDir $file)
        }
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
