# Build the prism-server onedir bundle and stage it under the desktop project.
# Usage:  pwsh scripts/build-server.ps1
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

pip install pyinstaller | Out-Host

# Clean prior outputs so stale files don't linger in the bundle.
Remove-Item -Recurse -Force "$repo\build\prism-server" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$repo\dist\prism-server" -ErrorAction SilentlyContinue

pyinstaller --noconfirm --clean prism-server.spec | Out-Host

$dest = "$repo\desktop\src-tauri\binaries\prism-server"
Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
Copy-Item -Recurse -Force "$repo\dist\prism-server" $dest

Write-Host "Built: $dest\prism-server.exe"
