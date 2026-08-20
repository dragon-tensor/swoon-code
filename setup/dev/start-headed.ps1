param([string]$Name = "dev")

$setupDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$projectRoot = Split-Path -Parent $setupDir
$env:SWOON_WORK_ROOT = Join-Path $projectRoot "work"
if (-not $env:SWOON_COOKIE_FILE) {
    $env:SWOON_COOKIE_FILE = Join-Path $projectRoot "codebase\cookies.json"
}
& (Join-Path $setupDir ".runtime\venv\Scripts\python.exe") -m swoon $Name --headed --verbose
