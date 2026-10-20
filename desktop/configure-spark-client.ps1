#Requires -Version 5.1
param(
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 7000
)

$ErrorActionPreference = "Stop"
$configDirectory = Join-Path $env:APPDATA "Prism"
$configPath = Join-Path $configDirectory "client.json"
$config = @{
    # localhost resolves to the IPv6-only tunnel first and is accepted as a
    # narrow Tauri remote-capability origin for native Model IDE commands.
    url = "http://localhost:$LocalPort"
    spawn_sidecar = $false
} | ConvertTo-Json

New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($configPath, $config, $utf8WithoutBom)

Write-Host "Prisma Desktop is configured for the Spark SSH tunnel." -ForegroundColor Green
Write-Host "Config: $configPath"
Write-Host ""
Write-Host "Keep this tunnel open before launching Prisma Desktop:"
Write-Host "  ssh -L `"[::1]:${LocalPort}:127.0.0.1:7000`" sparky@spark-132a"
