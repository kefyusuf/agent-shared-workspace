# Single Windows entry point. All behavior is implemented and tested in Python.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Python launcher (py) not found. Install Python 3.11+ and retry.'
}
& py -3 (Join-Path $root 'scripts/workspace_cli.py') @args
exit $LASTEXITCODE
