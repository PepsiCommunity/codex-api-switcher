$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript = Join-Path $scriptDir "codex_provider_menu.py"

if (-not (Test-Path -LiteralPath $pyScript)) {
    Write-Host "codex_provider_menu.py not found next to this .ps1 file." -ForegroundColor Red
    Write-Host "Expected: $pyScript"
    Read-Host "Press Enter to close"
    exit 1
}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source -3 $pyScript
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source $pyScript
    exit $LASTEXITCODE
}

Write-Host "Python was not found. Install Python or add it to PATH." -ForegroundColor Red
Read-Host "Press Enter to close"
exit 1
