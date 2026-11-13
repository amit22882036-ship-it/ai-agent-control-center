# Starts the Control Center from this repository's current code, for trying fixes before an installer exists.
#
#   .\scripts\start-dev.ps1        (or double-click scripts\start-dev.cmd)
#
# - Its data is separate from the installed app: the repository's development database
#   (data\control_center.sqlite3, git-ignored), with Task workspaces and Trash in
#   %LOCALAPPDATA%\AI Agent Control Center (Dev). The installed app's data is never read or changed.
# - Backend on http://127.0.0.1:8100, interface on http://127.0.0.1:5180, so the installed app
#   (which picks its own port) can stay installed. Do not work on the same project in both at once.
# - Real provider CLIs (Codex, Claude Code, Gemini CLI) work as in the installed app, with your own
#   sign-in and usage. Connections settings belong to each database, so confirm them once here too.
# - Two windows open: "Control Center Dev - backend" and "Control Center Dev - interface". Close
#   both to stop. After a code change: the interface reloads by itself; for a backend change, close
#   the backend window and run this again (the backend does not reload by itself, because a reload
#   would stop agents that are running).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$data = Join-Path $env:LOCALAPPDATA 'AI Agent Control Center (Dev)'
$database = Join-Path $root 'data\control_center.sqlite3'
$backendPort = 8100
$interfacePort = 5180
$interfaceUrl = "http://127.0.0.1:$interfacePort/"

function Test-Listening($port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

function Start-Window($title, $directory, $script) {
    # The script runs in its own PowerShell window; EncodedCommand keeps paths with spaces intact.
    $full = "`$host.UI.RawUI.WindowTitle = '$title'`n" + "Set-Location -LiteralPath '$directory'`n" + $script
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($full))
    Start-Process powershell.exe -ArgumentList '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded | Out-Null
}

New-Item -ItemType Directory -Force -Path $data | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $database) | Out-Null

# First run on this computer: the Python environment and the interface packages.
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Creating the Python environment (first run only)...'
    python -m venv (Join-Path $root '.venv')
    & $python -m pip install --disable-pip-version-check -q -r (Join-Path $root 'backend\requirements.txt')
}
if (-not (Test-Path (Join-Path $root 'frontend\node_modules'))) {
    Write-Host 'Installing the interface packages (first run only)...'
    Push-Location (Join-Path $root 'frontend'); npm ci; Pop-Location
}

if (Test-Listening $backendPort) {
    Write-Host "The development backend already runs on port $backendPort (window 'Control Center Dev - backend')."
} else {
    $backend = @"
`$env:CONTROL_CENTER_DB_PATH = '$database'
`$env:CONTROL_CENTER_WORKSPACE_ROOT = '$data\task-workspaces'
`$env:CONTROL_CENTER_TRASH_ROOT = '$data\Trash'
`$env:CONTROL_CENTER_ALLOWED_ORIGINS = 'http://127.0.0.1:$interfacePort,http://localhost:$interfacePort'
Write-Host 'Control Center Dev backend. Database: $database. Close this window to stop it.'
& '$python' -m uvicorn app.main:app --host 127.0.0.1 --port $backendPort
"@
    Start-Window 'Control Center Dev - backend' (Join-Path $root 'backend') $backend
}

if (Test-Listening $interfacePort) {
    Write-Host "The development interface already runs on port $interfacePort."
} else {
    $interface = @"
`$env:VITE_API_BASE = 'http://127.0.0.1:$backendPort'
Write-Host 'Control Center Dev interface. Close this window to stop it.'
npx vite --host 127.0.0.1 --port $interfacePort --strictPort
"@
    Start-Window 'Control Center Dev - interface' (Join-Path $root 'frontend') $interface
}

# Open the browser once both answer (up to a minute on a first start).
$deadline = (Get-Date).AddSeconds(60)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$backendPort/health" | Out-Null
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $interfaceUrl | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 700
    }
}
if ($ready) {
    Start-Process $interfaceUrl
    Write-Host "Control Center Dev is open at $interfaceUrl"
} else {
    Write-Host "Control Center Dev did not answer within a minute. Check the two 'Control Center Dev' windows for errors."
}
