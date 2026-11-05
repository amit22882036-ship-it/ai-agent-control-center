# Builds the Windows installer: interface, packaged backend, then the Electron app and its NSIS
# installer. Run from the repository root in PowerShell:  .\desktop\build.ps1
# Requires Python 3.11 and Node.js 20 or later. Nothing is installed outside the repository.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host '1/4 Interface (desktop build)'
Push-Location frontend
npm ci
npm run build:desktop
Pop-Location

Write-Host '2/4 Backend build environment'
$venv = Join-Path $root 'desktop\.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) { python -m venv $venv }
& (Join-Path $venv 'Scripts\python.exe') -m pip install --disable-pip-version-check -q -r backend\requirements.txt pyinstaller==6.22.3

Write-Host '3/4 Packaged backend'
& (Join-Path $venv 'Scripts\python.exe') -m PyInstaller desktop\backend.spec --noconfirm --clean `
  --distpath backend\dist-desktop --workpath desktop\.pyi-work

Write-Host '4/4 Desktop app and installer'
Push-Location desktop
npm ci
npm run dist
Pop-Location
Write-Host "Installer: $(Join-Path $root 'desktop\out')"
