$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

function Set-StartupProgress {
    param([int]$Value, [string]$Message)
    if ($env:H3_STARTUP_PROGRESS) {
        [System.IO.File]::WriteAllText($env:H3_STARTUP_PROGRESS, "$Value|$Message", [System.Text.Encoding]::UTF8)
    }
}
Set-StartupProgress 5 '正在检查工作台…'
$healthPath = '/api/system/project-status'

# **首选端口改成 15111（ComfyUI 同理改成 15188）** —— 用户 2026-09-19 明确要求：
# 「直接帮我改掉 不要用这些端口了 改成系统不要的」。原来首选 8111/8188，但这两个端口
# 落在 Windows 的**保留（排除）区间**里，每次开机都可能绑不上：
#   uvicorn 报 `[Errno 13] ... [winerror 10013]` —— 注意 10013 是"拒绝访问"，
#   **不是"端口被占用"**（被占用是 10048），所以旧提示"端口已被其它程序占用"是误导。
# 根因：本机 TCP 动态端口范围被改成了 1024~15000（`netsh int ipv4 show dynamicport tcp`），
# Hyper-V/WSL 的 NAT（hns/winnat）会从这段范围按 100 个一组预留，8014-8113 与 8114-8213
# 就是这么来的（`netsh interface ipv4 show excludedportrange protocol=tcp`），8111/8188
# 正好被圈进去；而且这些预留**每次开机重新分配**（历史上本项目已因此从 8011 搬到 8111）。
# **15000 以上不在动态端口范围内，NAT 不会再预留**，所以直接把首选放在 15000 以上，
# 低端口不再尝试；后面仍留两个兜底，顺序固定 ⇒ 地址稳定、书签不会天天变。
$portCandidates = @(15111, 15222, 18333)
if ($env:H3_PORT) { $portCandidates = @([int]$env:H3_PORT) + $portCandidates }

function Get-SocketErrorCode {
    param($ErrorRecord)
    $exception = $ErrorRecord.Exception
    while ($exception) {
        if ($exception -is [System.Net.Sockets.SocketException]) { return [int]$exception.ErrorCode }
        $exception = $exception.InnerException
    }
    return -1
}

# 试绑一次来判断这个端口到底能不能用，并把失败原因（错误码）带出来。
function Test-PortBind {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return @{ Port = $Port; Ok = $true; ErrorCode = 0 }
    } catch {
        return @{ Port = $Port; Ok = $false; ErrorCode = (Get-SocketErrorCode $_) }
    } finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

function Format-BindFailure {
    param($Probe)
    switch ($Probe.ErrorCode) {
        10013 { return ('{0}：被系统保留（winerror 10013，Hyper-V/WSL 预留的端口区间）' -f $Probe.Port) }
        10048 { return ('{0}：已被其它程序占用（winerror 10048）' -f $Probe.Port) }
        default { return ('{0}：无法绑定（winerror {1}）' -f $Probe.Port, $Probe.ErrorCode) }
    }
}

function Test-ServiceReady {
    param([string]$Url, [int]$TimeoutSeconds = 5)
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSeconds ($Url + $healthPath)
        return $true
    } catch {
        return $false
    }
}

# 端口上**有没有人在监听**（用来找本机代理：连得上 = 有服务）。
function Test-PortListening {
    param([int]$Port, [string]$TargetHost = '127.0.0.1')
    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(250)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) { try { $client.Close() } catch { } }
    }
}

# ComfyUI 的健康检查走 `/system_stats`（后端那套 `/api/config` 是它自己的）。
function Test-ComfyReady {
    param([string]$Url, [int]$TimeoutSeconds = 3)
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSeconds ($Url + '/system_stats')
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

# 已在运行则直接打开浏览器，避免再起一个实例抢端口。逐个候选端口探测：
# 上一次可能是靠兜底端口起来的。
foreach ($port in $portCandidates) {
    $existingUrl = 'http://127.0.0.1:' + $port
    if (Test-ServiceReady -Url $existingUrl) {
        Write-Host ('检测到工作台已在运行（' + $existingUrl + '），直接打开窗口。')
        Start-Process $existingUrl
        Set-StartupProgress 100 '工作台已就绪'
        exit 0
    }
}

Set-StartupProgress 15 '正在检查本地环境…'
# 选一个能绑的端口：先 8111，不可用就换 15000 以上的稳定端口。
$servicePort = 0
$failed = @()
foreach ($port in $portCandidates) {
    $probe = Test-PortBind -Port $port
    if ($probe.Ok) { $servicePort = $port; break }
    $failed += (Format-BindFailure -Probe $probe)
}
if ($servicePort -eq 0) {
    Write-Host '启动失败：所有候选端口都无法绑定。' -ForegroundColor Red
    foreach ($line in $failed) { Write-Host ('  - ' + $line) -ForegroundColor Yellow }
    Write-Host '排查命令：' -ForegroundColor Yellow
    Write-Host '  端口是否被占用： netstat -ano | findstr :8111' -ForegroundColor Yellow
    Write-Host '  是否被系统保留： netsh interface ipv4 show excludedportrange protocol=tcp' -ForegroundColor Yellow
    Write-Host '  动态端口范围：   netsh int ipv4 show dynamicport tcp' -ForegroundColor Yellow
    Write-Host '  指定端口启动：   set H3_PORT=<端口> 后再运行本脚本' -ForegroundColor Yellow
    exit 1
}
$serviceUrl = 'http://127.0.0.1:' + $servicePort
if ($servicePort -ne 15111) {
    Write-Host ('15111 不可用，改用 ' + $servicePort + '。') -ForegroundColor Yellow
    foreach ($line in $failed) { Write-Host ('  - ' + $line) -ForegroundColor DarkGray }
}

# ComfyUI 的端口同理：**首选 15188**（15000 以上不在 Windows 动态端口范围内，NAT 不会预留），
# 不再先试会被保留区间圈住的 8188。端口由 settings.COMFY_PORT 从 H3_COMFY_URL 解析
# （pipeline 拉起 ComfyUI 时用同一个值），所以这里选好之后必须把 H3_COMFY_URL 一起交给后端 ——
# 否则「后端起在 15188、ComfyUI 却监听 8188」，健康检查永远不过。
# 用户自己配过 H3_COMFY_URL 就以他的为准，不覆盖。
if ($env:H3_COMFY_URL) {
    Write-Host ('ComfyUI 使用已配置的地址：' + $env:H3_COMFY_URL) -ForegroundColor DarkGray
} else {
    # ① **已经在跑就沿用它**：ComfyUI 常驻时端口当然"绑不上"，那是它在监听，不是被系统保留。
    #    2026-09-20 实测事故：后端重启时 ComfyUI 正在渲染，这里误判成「15188 不可用」退回 18188，
    #    把 H3_COMFY_URL 指到一个没人监听的端口 → 后端看不见正在跑的 prompt，重启恢复逻辑
    #    只能把那条渲染标成「中断」（白跑一条）。所以先问健康、再谈绑端口。
    $comfyPort = 0
    $comfyFailed = @()
    foreach ($port in @(15188, 18188)) {
        if (Test-ComfyReady -Url ('http://127.0.0.1:' + $port)) { $comfyPort = $port; break }
    }
    if ($comfyPort -ne 0) {
        Write-Host ('检测到 ComfyUI 已在运行（127.0.0.1:' + $comfyPort + '），沿用这个端口。') -ForegroundColor DarkGray
        $env:H3_COMFY_URL = 'http://127.0.0.1:' + $comfyPort
    } else {
        foreach ($port in @(15188, 18188)) {
            $probe = Test-PortBind -Port $port
            if ($probe.Ok) { $comfyPort = $port; break }
            $comfyFailed += (Format-BindFailure -Probe $probe)
        }
        if ($comfyPort -eq 0) {
            Write-Host '警告：15188 与 18188 都无法绑定，ComfyUI 可能起不来。' -ForegroundColor Yellow
            foreach ($line in $comfyFailed) { Write-Host ('  - ' + $line) -ForegroundColor DarkGray }
        } else {
            $env:H3_COMFY_URL = 'http://127.0.0.1:' + $comfyPort
            if ($comfyPort -ne 15188) {
                Write-Host ('15188 不可用，ComfyUI 改用 ' + $comfyPort + '。') -ForegroundColor Yellow
                foreach ($line in $comfyFailed) { Write-Host ('  - ' + $line) -ForegroundColor DarkGray }
            }
        }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules'))) {
    npm install --prefer-offline --no-audit --no-fund
}

# 中转站（出图）**直连不通时必须走代理** —— 2026-09-22 事故：`api.teamorouter.com`
# 被解析到无效 IP，后端 httpx 直连全部超时，出图失败后按设计退回"源视频取帧"，
# 用户看到的是「人物图 = 抽帧图」。后端自己也会自动探测（见 backend\netproxy.py），
# 这里额外把结果写进进程环境变量，让链路更明确、也方便排查。
# ⚠️ 只设**后端进程**的环境变量，不动用户的注册表与系统代理设置。
if ($env:H3_IMAGE_PROXY) {
    Write-Host ('出图代理按已配置的值：' + $env:H3_IMAGE_PROXY) -ForegroundColor DarkGray
} else {
    foreach ($proxyPort in @(7897, 7890, 10809, 1080)) {
        if (Test-PortListening -Port $proxyPort) {
            $env:H3_IMAGE_PROXY = 'http://127.0.0.1:' + $proxyPort
            Write-Host ('检测到本机代理（127.0.0.1:' + $proxyPort + '），出图请求走它。') -ForegroundColor DarkGray
            break
        }
    }
    if (-not $env:H3_IMAGE_PROXY) {
        Write-Host '提示：未检测到本机代理端口；若出图报"连接失败"而人物图变成抽帧图，' -ForegroundColor Yellow
        Write-Host '      请确认代理软件在跑，或设置 H3_IMAGE_PROXY=http://127.0.0.1:端口 后重启。' -ForegroundColor Yellow
    }
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    python -m venv (Join-Path $projectRoot '.venv')
}

Set-StartupProgress 30 '正在准备运行依赖…'
Write-Host '正在准备后端依赖……'
& $venvPython -m pip install -r (Join-Path $projectRoot 'backend\requirements.txt') --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw '后端依赖准备失败，请查看启动日志。' }
Set-StartupProgress 55 '正在准备工作台页面…'
Write-Host '正在构建前端页面……'
npm run build

if ($LASTEXITCODE -ne 0) { throw '工作台页面构建失败，请查看启动日志。' }
Set-StartupProgress 80 '正在启动本地服务…'
Write-Host '正在启动本地服务……'
# 后端输出必须落盘：2026-09-13 页面上出现过「Unexpected token 'I', "Internal S"...」，
# 但当时的实例没有重定向输出，500 的 traceback 直接丢了，只能靠猜。
$logDir = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
# 后端有一批"调用自己"的链路（batch_worker / publish_watcher / chatgpt_image 都走
# settings.BATCH_SELF_URL），端口换掉时必须一起告诉它，否则这些自调用会打到空端口。
$env:H3_SELF_URL = $serviceUrl
$backend = Start-Process -FilePath $venvPython `
    -ArgumentList @('-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', "$servicePort") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'backend-stdout.log') `
    -RedirectStandardError (Join-Path $logDir 'backend-stderr.log') `
    -PassThru

try {
    $ready = $false
    # 就绪检查：ComfyUI 未启动时 /api/config 可能耗时数秒（健康检查要
    # 等待 8188 端口超时），单次探测超时必须给足 5 秒，否则会误判失败。
    for ($attempt = 1; $attempt -le 24; $attempt++) {
        if ($backend.HasExited) {
            Write-Host ('本地服务进程意外退出（退出码 ' + $backend.ExitCode + '）。') -ForegroundColor Red
            Write-Host ('真实原因在日志里：' + (Join-Path $logDir 'backend-stderr.log')) -ForegroundColor Yellow
            Write-Host '  查看最后几行： Get-Content data\backend-stderr.log -Tail 20' -ForegroundColor Yellow
            Write-Host ('  若含 "winerror 10013"：端口 ' + $servicePort + ' 被系统保留；' +
                '若含 "winerror 10048"：端口被其它程序占用。') -ForegroundColor Yellow
            Write-Host '  可换端口重试： set H3_PORT=15111 后再运行本脚本' -ForegroundColor Yellow
            exit 1
        }
        if (Test-ServiceReady -Url $serviceUrl) {
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
        Write-Host ('  1. 后端是否报错：Get-Content ' + (Join-Path $logDir 'backend-stderr.log') + ' -Tail 20') -ForegroundColor Yellow
        Write-Host ('  2. 端口 ' + $servicePort + ' 是否被占用：netstat -ano | findstr :' + $servicePort) -ForegroundColor Yellow
        Write-Host ('  3. 是否被系统保留：netsh interface ipv4 show excludedportrange protocol=tcp') -ForegroundColor Yellow
        exit 1
    }

    Write-Host '' -ForegroundColor Green
    Write-Host ('启动成功！正在打开工作台：' + $serviceUrl) -ForegroundColor Green
    Write-Host '提示：关闭本窗口会同时停止工作台服务。' -ForegroundColor DarkGray
    Write-Host ''
    Set-StartupProgress 100 '工作台已就绪'
    Start-Process $serviceUrl
    Wait-Process -Id $backend.Id
} finally {
    if (-not $backend.HasExited) {
        Stop-Process -Id $backend.Id -Force
    }
}
