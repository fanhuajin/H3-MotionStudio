$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules'))) {
    npm install --prefer-offline --no-audit --no-fund
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $projectRoot '.venv')
}

& $venvPython -m pip install -r (Join-Path $projectRoot 'backend\requirements.txt') --disable-pip-version-check
npm run build

$backend = Start-Process -FilePath $venvPython `
    -ArgumentList @('-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '8011') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8011/api/config'
            $ready = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        throw 'H3 MotionStudio 本地服务启动失败。'
    }
    Start-Process 'http://127.0.0.1:8011'
    Wait-Process -Id $backend.Id
} finally {
    if (-not $backend.HasExited) {
        Stop-Process -Id $backend.Id -Force
    }
}

