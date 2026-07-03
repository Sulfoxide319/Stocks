param(
    [string]$InstallDir = "$env:LOCALAPPDATA\StocksTradingAssistant",
    [string]$Repository = "Sulfoxide319/Stocks",
    [string]$GitHubApiBase = "https://api.github.com",
    [int]$QuietCheckIntervalHours = 6,
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

function Get-GitHubHeaders {
    param([string]$Accept = "application/vnd.github+json")
    $headers = @{
        "User-Agent" = "StocksTradingAssistant-Updater"
        "Accept" = $Accept
    }
    $token = $env:GITHUB_TOKEN
    if (-not $token) {
        $token = $env:GH_TOKEN
    }
    if ($token) {
        $headers["Authorization"] = "Bearer $token"
        $headers["X-GitHub-Api-Version"] = "2022-11-28"
    }
    return $headers
}

function Invoke-GitHubGet {
    param([string]$Uri)
    return Invoke-RestMethod -Uri $Uri -Headers (Get-GitHubHeaders)
}

function Invoke-GitHubWebGet {
    param([string]$Uri)
    return Invoke-WebRequest -Uri $Uri -Headers (Get-GitHubHeaders "text/html") -UseBasicParsing
}

function Get-ResponseUriText {
    param([object]$Response)
    if ($Response.BaseResponse.ResponseUri) {
        return [string]$Response.BaseResponse.ResponseUri
    }
    if ($Response.BaseResponse.RequestMessage -and $Response.BaseResponse.RequestMessage.RequestUri) {
        return [string]$Response.BaseResponse.RequestMessage.RequestUri
    }
    return ""
}

function Convert-HtmlDecoded {
    param([string]$Value)
    return [System.Net.WebUtility]::HtmlDecode($Value)
}

function Get-PublicGitHubRelease {
    $latestPage = "https://github.com/$Repository/releases/latest"
    $latestResponse = Invoke-GitHubWebGet $latestPage
    $latestUri = Get-ResponseUriText $latestResponse
    $tag = ""
    $tagMatch = [regex]::Match($latestUri, "/releases/tag/([^/?#]+)")
    if (-not $tagMatch.Success) {
        $tagMatch = [regex]::Match([string]$latestResponse.Content, "/$([regex]::Escape($Repository))/releases/tag/([^`"?#<]+)")
    }
    if ($tagMatch.Success) {
        $tag = Convert-HtmlDecoded $tagMatch.Groups[1].Value
    }
    if (-not $tag) {
        throw "Could not find the latest public GitHub release tag."
    }

    $assetsPage = "https://github.com/$Repository/releases/expanded_assets/$tag"
    $assetsResponse = Invoke-GitHubWebGet $assetsPage
    $assets = @()
    $pattern = 'href="/' + [regex]::Escape($Repository) + '/releases/download/' + [regex]::Escape($tag) + '/([^"]+)"'
    foreach ($match in [regex]::Matches([string]$assetsResponse.Content, $pattern)) {
        $name = [uri]::UnescapeDataString((Convert-HtmlDecoded $match.Groups[1].Value))
        $assets += [pscustomobject]@{
            name = $name
            browser_download_url = "https://github.com/$Repository/releases/download/$tag/$name"
            url = $null
        }
    }
    return [pscustomobject]@{
        tag_name = $tag
        assets = $assets
    }
}

function Get-GitHubErrorMessage {
    param([object]$ErrorRecord)
    $statusCode = $null
    $response = $ErrorRecord.Exception.Response
    if ($response -and $response.StatusCode) {
        $statusCode = [int]$response.StatusCode
    }
    if ($statusCode -eq 403) {
        return "GitHub update check was blocked or rate-limited (403). Set GITHUB_TOKEN/GH_TOKEN for private repos or wait for the API rate limit to reset."
    }
    if ($statusCode -eq 404) {
        return "GitHub release was not found (404). Check that $Repository exists, is accessible, and has a latest release."
    }
    return "Update check failed: $($ErrorRecord.Exception.Message)"
}

function Get-UpdateCachePath {
    return Join-Path $InstallDir ".update-check.json"
}

function Test-QuietCheckCache {
    if (-not $Quiet -or $Force -or $QuietCheckIntervalHours -le 0) {
        return $false
    }
    $cachePath = Get-UpdateCachePath
    if (-not (Test-Path $cachePath)) {
        return $false
    }
    try {
        $cache = Get-Content $cachePath -Raw | ConvertFrom-Json
        if ($cache.repository -ne $Repository) {
            return $false
        }
        $checkedAt = [datetime]::Parse([string]$cache.checked_at).ToUniversalTime()
        return $checkedAt -gt (Get-Date).ToUniversalTime().AddHours(-$QuietCheckIntervalHours)
    } catch {
        return $false
    }
}

function Save-UpdateCache {
    try {
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
        $cache = [ordered]@{
            repository = $Repository
            checked_at = (Get-Date).ToUniversalTime().ToString("o")
        }
        $cache | ConvertTo-Json -Depth 3 | Set-Content -Encoding UTF8 (Get-UpdateCachePath)
    } catch {
        # Cache failures should never prevent the app from starting.
    }
}

function Invoke-ReleaseAssetDownload {
    param(
        [object]$Asset,
        [string]$OutFile
    )
    $token = $env:GITHUB_TOKEN
    if (-not $token) {
        $token = $env:GH_TOKEN
    }
    if ($token -and $Asset.url) {
        Invoke-WebRequest -Uri $Asset.url -OutFile $OutFile -Headers (Get-GitHubHeaders "application/octet-stream")
        return
    }
    Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $OutFile -Headers (Get-GitHubHeaders)
}

$currentText = Get-InstalledVersion
$current = Convert-ToVersion $currentText
$releaseUri = "$($GitHubApiBase.TrimEnd('/'))/repos/$Repository/releases/latest"

if (Test-QuietCheckCache) {
    Write-UpdateLog "Update check skipped: checked recently."
    exit 0
}

try {
    $release = Invoke-GitHubGet $releaseUri
} catch {
    $apiError = Get-GitHubErrorMessage $_
    Write-UpdateLog "$apiError Trying public GitHub Releases page..."
    try {
        $release = Get-PublicGitHubRelease
    } catch {
        Write-UpdateLog "Update check failed: $($_.Exception.Message)"
        Save-UpdateCache
        exit 0
    }
}

$latestText = [string]$release.tag_name
$latest = Convert-ToVersion $latestText
if (-not $Force -and $latest -le $current) {
    Write-UpdateLog "Already up to date: $currentText"
    Save-UpdateCache
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
    Invoke-ReleaseAssetDownload -Asset $asset -OutFile $zipPath
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
    Save-UpdateCache
} catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
}
