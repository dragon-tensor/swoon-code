param(
    [string]$Cookies = ""
)

$ErrorActionPreference = "Stop"
$setupDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$projectRoot = Split-Path -Parent $setupDir
$codebaseDir = Join-Path $projectRoot "codebase"
$runtimeDir = Join-Path $setupDir ".runtime"
$venvDir = Join-Path $runtimeDir "venv"
$workDir = Join-Path $projectRoot "work"
$configDir = Join-Path $env:APPDATA "Swoon Code"
$launcherDir = Join-Path $env:LOCALAPPDATA "Swoon Code\bin"
$launcher = Join-Path $launcherDir "swoon.cmd"

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --exact --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
}
if (-not $pythonCommand) {
    throw "Python 3.11 or newer is required. Install Python and rerun setup."
}

New-Item -ItemType Directory -Force -Path $runtimeDir, (Join-Path $workDir "input"), (Join-Path $workDir "output"), $configDir, $launcherDir | Out-Null
& $pythonCommand.Source -m venv $venvDir
$venvPython = Join-Path $venvDir "Scripts\python.exe"
& $venvPython -m pip install $codebaseDir
& $venvPython -m playwright install chromium

if (-not $Cookies) {
    $legacyCookies = Join-Path $codebaseDir "cookies.json"
    if (Test-Path -LiteralPath $legacyCookies -PathType Leaf) {
        $Cookies = $legacyCookies
    } else {
        $Cookies = Read-Host "Path to your exported chatgpt.com cookies.json"
    }
}
if (-not (Test-Path -LiteralPath $Cookies -PathType Leaf)) {
    throw "Cookie file not found: $Cookies"
}
$cookieTarget = Join-Path $configDir "cookies.json"
Copy-Item -LiteralPath $Cookies -Destination $cookieTarget -Force

@"
@echo off
set "SWOON_WORK_ROOT=$workDir"
set "SWOON_COOKIE_FILE=$cookieTarget"
"$venvPython" -m swoon %*
"@ | Set-Content -LiteralPath $launcher -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ";") -notcontains $launcherDir) {
    $updatedPath = if ($userPath) { "$userPath;$launcherDir" } else { $launcherDir }
    [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
}

Write-Host "Swoon Code is installed."
Write-Host "Open a new terminal and run: swoon"
Write-Host "For a named workspace run: swoon my-project"
Write-Host "The agent keeps Chromium open for human verification; optional refresh: swoon auth"
