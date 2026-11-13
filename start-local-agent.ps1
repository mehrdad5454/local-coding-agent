<#
.SYNOPSIS
    Generic launcher for Local Agent (llama.cpp model server + Flask web UI).
.DESCRIPTION
    Contains NO personal paths. Everything is configurable.

    Configuration resolution order (highest priority first):
      1. Environment variables (e.g. $env:MODEL_PATH)
      2. local.config.json next to this script (git-ignored)
      3. Built-in defaults

    See local.config.example.json for the full list of keys.

    Examples:
      .\start-local-agent.ps1 -DryRun        # print resolved plan, change nothing
      .\start-local-agent.ps1                # normal launch
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# ---------------------------------------------------------------- config ----
$config = @{}
$configPath = Join-Path $root 'local.config.json'
if (Test-Path -LiteralPath $configPath) {
    try {
        $raw = Get-Content -LiteralPath $configPath -Raw
        if ($raw.Trim()) {
            $config = ConvertFrom-Json -InputObject $raw -AsHashtable
        }
    }
    catch {
        Write-Warning "local.config.json is not valid JSON; ignoring it."
    }
}

function Resolve-Config {
    param([string]$Name, [string]$Default = '')
    $envValue = [Environment]::GetEnvironmentVariable($Name)
    if ($envValue) { return $envValue }
    if ($config -and $config.Contains($Name) -and $config[$Name]) { return [string]$config[$Name] }
    return $Default
}

$llamaExe    = Resolve-Config 'LLAMA_SERVER_EXE' 'llama-server.exe'
$modelPath   = Resolve-Config 'MODEL_PATH' ''
$modelHost   = Resolve-Config 'MODEL_HOST' '127.0.0.1'
$modelPort   = Resolve-Config 'MODEL_PORT' '8080'
$modelCtx    = Resolve-Config 'MODEL_CONTEXT' '8192'
$pythonExe   = Resolve-Config 'PYTHON_EXE' (Join-Path $root '.venv\Scripts\python.exe')
$webPort     = Resolve-Config 'WEB_PORT' '5000'
$openBrowser = (Resolve-Config 'OPEN_BROWSER' '1') -notin @('0', 'false', 'no')

# ------------------------------------------------------------- validation ---
$problems = @()
if (-not $modelPath) {
    $problems += 'MODEL_PATH is not set (env var or local.config.json). See local.config.example.json.'
}
elseif (-not (Test-Path -LiteralPath $modelPath)) {
    $problems += "MODEL_PATH does not exist: $modelPath"
}
if (-not (Get-Command $llamaExe -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $llamaExe)) {
    $problems += "llama-server not found: $llamaExe (set LLAMA_SERVER_EXE or add it to PATH)"
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $problems += "Python not found: $pythonExe (create .venv first, see README)"
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'web_agent.py'))) {
    $problems += 'web_agent.py not found next to this script'
}
if ($problems.Count -gt 0) {
    Write-Host 'Cannot start Local Agent:' -ForegroundColor Red
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

# ------------------------------------------------------------------ plan ----
Write-Host 'Local Agent launcher' -ForegroundColor Cyan
Write-Host "  llama-server : $llamaExe"
Write-Host "  model        : $modelPath"
Write-Host "  model listen : http://${modelHost}:$modelPort"
Write-Host "  context      : $modelCtx"
Write-Host "  python       : $pythonExe"
Write-Host "  web ui       : http://127.0.0.1:$webPort"
Write-Host "  open browser : $openBrowser"

if ($DryRun) {
    Write-Host 'Dry run: nothing was started or stopped.' -ForegroundColor Yellow
    exit 0
}

# --------------------------------------------------- stop old instances -----
taskkill /F /IM python.exe 2>$null | Out-Null
taskkill /F /IM llama-server.exe 2>$null | Out-Null
Start-Sleep -Milliseconds 800

# --------------------------------------------------------- llama-server -----
$llamaArgs = @('-m', $modelPath, '--host', $modelHost, '--port', $modelPort, '-c', $modelCtx)
Start-Process -FilePath $llamaExe -ArgumentList $llamaArgs -WindowStyle Hidden

$modelOk = $false
foreach ($i in 1..60) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-RestMethod -Uri "http://${modelHost}:$modelPort/health" -TimeoutSec 2
        if ($r.status -eq 'ok') { $modelOk = $true; break }
    }
    catch { }
}
if (-not $modelOk) {
    Write-Host "llama-server did not become healthy on http://${modelHost}:$modelPort" -ForegroundColor Red
    exit 1
}
Write-Host 'Model server is ready.' -ForegroundColor Green

# ------------------------------------------------------------ web agent -----
Start-Process -FilePath $pythonExe `
    -ArgumentList (Join-Path $root 'web_agent.py') `
    -WorkingDirectory $root -WindowStyle Hidden

$webOk = $false
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$webPort/" -TimeoutSec 2 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { $webOk = $true; break }
    }
    catch { }
}
if (-not $webOk) {
    Write-Host "Web UI did not start on http://127.0.0.1:$webPort" -ForegroundColor Red
    exit 1
}
Write-Host 'Web UI is ready.' -ForegroundColor Green

if ($openBrowser) {
    Start-Process "http://127.0.0.1:$webPort/"
}
Write-Host 'Local Agent is running. Press Ctrl+C in the model/web windows to stop them.' -ForegroundColor Green