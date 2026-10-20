$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$mediaTools = Join-Path (Get-Location) 'data/tools/ffmpeg'
if ((Test-Path "$mediaTools/ffmpeg.exe") -and (Test-Path "$mediaTools/ffprobe.exe")) {
    $env:PATH = $mediaTools + [IO.Path]::PathSeparator + $env:PATH
}
$checkPython = Join-Path (Get-Location) '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $checkPython)) { $checkPython = 'python' }
& $checkPython -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython -m ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython -m mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython scripts/check_frontend.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython scripts/check_js.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $checkPython -m pytest -q
exit $LASTEXITCODE
