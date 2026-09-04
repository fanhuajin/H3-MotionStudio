$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

$serviceUrl = 'http://127.0.0.1:8111'
$healthPath = '/api/config'

function Test-ServiceReady {
    param([int]$TimeoutSeconds = 5)
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSeconds ($serviceUrl + $healthPath)
        return $true
    } catch {
        return $false
    }
}

Write-Host ''
Write-Host '============================================'
Write-Host '   H3 影动高清工作台 - 正在启动'
Write-Host '============================================'
Write-Host ''

# 已在运行则直接打开浏览器，避免再起一个实例抢 8111 端口
if (Test-ServiceReady) {
    Write-Host ('检测到工作台已在运行（' + $serviceUrl + '），直接打开窗口。')
    Start-Process $serviceUrl
    exit 0
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules'))) {
    npm install --prefer-offline --no-audit --no-fund
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $projectRoot '.venv')
}

Write-Host '正在准备后端依赖……'
& $venvPython -m pip install -r (Join-Path $projectRoot 'backend\requirements.txt') --disable-pip-version-check
Write-Host '正在构建前端页面……'
npm run build

Write-Host '正在启动本地服务……'
$backend = Start-Process -FilePath $venvPython `
    -ArgumentList @('-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '8111') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

try {
    $ready = $false
    # 就绪检查：ComfyUI 未启动时 /api/config 可能耗时数秒（健康检查要
    # 等待 8188 端口超时），单次探测超时必须给足 5 秒，否则会误判失败。
    for ($attempt = 1; $attempt -le 24; $attempt++) {
        if ($backend.HasExited) {
            Write-Host ('本地服务进程意外退出（退出码 ' + $backend.ExitCode + '）。') -ForegroundColor Red
            Write-Host '可能原因：8111 端口已被其它程序占用。' -ForegroundColor Yellow
            Write-Host ('可运行命令检查：netstat -ano | findstr :8111') -ForegroundColor Yellow
            exit 1
        }
        if (Test-ServiceReady) {
            $ready = $true
            break
        }
        Write-Host ('等待服务就绪……（{0}/24）' -f $attempt)
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        Write-Host '' -ForegroundColor Red
        Write-Host '启动失败：服务在等待 24 次后仍无响应。' -ForegroundColor Red
        Write-Host '请检查：' -ForegroundColor Yellow
        Write-Host ('  1. 8111 端口是否被占用：netstat -ano | findstr :8111') -ForegroundColor Yellow
        Write-Host '  2. 后端是否报错：在项目目录手动运行' -ForegroundColor Yellow
        Write-Host '     .venv\Scripts\python.exe -m uvicorn backend.app:app --port 8111' -ForegroundColor Yellow
        exit 1
    }

    Write-Host '' -ForegroundColor Green
    Write-Host ('启动成功！正在打开工作台：' + $serviceUrl) -ForegroundColor Green
    Write-Host '提示：关闭本窗口会同时停止工作台服务。' -ForegroundColor DarkGray
    Write-Host ''
    Start-Process $serviceUrl
    Wait-Process -Id $backend.Id
} finally {
    if (-not $backend.HasExited) {
        Stop-Process -Id $backend.Id -Force
    }
}
