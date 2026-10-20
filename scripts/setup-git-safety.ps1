$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
git -C $repositoryRoot config --local core.hooksPath .githooks
git -C $repositoryRoot config --local pull.ff only
git -C $repositoryRoot config --local fetch.prune true
git -C $repositoryRoot config --local push.autoSetupRemote true

$gitleaksCommand = Get-Command gitleaks -ErrorAction SilentlyContinue
if ($gitleaksCommand) {
    $gitleaksPath = $gitleaksCommand.Source
} else {
    $winGetRoot = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $gitleaksPath = Get-ChildItem -LiteralPath $winGetRoot -Filter gitleaks.exe -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $gitleaksPath) {
    throw "Gitleaks was not found. Install it with: winget install --id Gitleaks.Gitleaks --exact"
}

$portableGitleaksPath = $gitleaksPath.Replace("\", "/")
git -C $repositoryRoot config --local prisma.gitleaksPath $portableGitleaksPath

Write-Host "Git safety enabled for $repositoryRoot"
Write-Host "- direct pushes to main are blocked"
Write-Host "- every push scans Git history with Gitleaks"
Write-Host "- pulls are fast-forward only"
