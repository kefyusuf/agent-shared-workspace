# Interactive, process-scoped launcher. This does not install or modify MCP config.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('codex', 'claude')]
    [string] $Provider,
    [ValidateRange(1,65535)]
    [int] $Port = 8765
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$oldCredential = [Environment]::GetEnvironmentVariable('AW_AGENT_CREDENTIAL', 'Process')
$oldPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$oldPort = [Environment]::GetEnvironmentVariable('AW_BROKER_PORT', 'Process')
$ptr = [IntPtr]::Zero
try {
    if (-not (Get-Command $Provider -ErrorAction SilentlyContinue)) {
        throw "$Provider CLI not found in PATH. Install and configure it first."
    }
    if (-not (Get-Command 'py' -ErrorAction SilentlyContinue)) {
        throw 'Python py launcher not found in PATH.'
    }
    $secure = Read-Host "Enter the $Provider AGENT credential (never operator credential)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $env:AW_AGENT_CREDENTIAL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    $ptr = [IntPtr]::Zero
    Remove-Variable secure -ErrorAction SilentlyContinue
    $env:AW_BROKER_PORT = [string]$Port
    $env:PYTHONPATH = (Join-Path $root 'src')
    Push-Location $root
    try {
        & py (Join-Path $root 'scripts/host_preflight.py') --host $Provider --port $Port
        if ($LASTEXITCODE -ne 0) { throw 'MCP preflight failed. No agent was launched.' }
        Write-Host "Preflight passed (NOT a real host compatibility proof). Launching $Provider..."
        & $Provider
    } finally {
        Pop-Location
    }
} finally {
    if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    [Environment]::SetEnvironmentVariable('AW_AGENT_CREDENTIAL', $oldCredential, 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $oldPath, 'Process')
    [Environment]::SetEnvironmentVariable('AW_BROKER_PORT', $oldPort, 'Process')
}
