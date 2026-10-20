$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$checkPython = Join-Path (Get-Location) '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $checkPython)) { $checkPython = 'python' }
& $checkPython -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython -m ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython -m mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython -m pytest -q
exit $LASTEXITCODE
