# 修复「端口被 Windows 保留区间抢占」导致 8111 / 8188 无法绑定。
#
# 症状：工作台后端报
#     [Errno 13] error while attempting to bind on address ('127.0.0.1', 8111): [winerror 10013]
#   ComfyUI 报
#     PermissionError: [Errno 13] ... ('127.0.0.1', 8188): [winerror 10013]
#   —— 注意是 10013（拒绝访问）而不是 10048（已被占用），所以 netstat 里看不到任何进程，
#   报「端口已被其它程序占用」是**误导**。
#
# 根因（2026-09-19 实测）：本机 TCP **动态端口范围被改成了 1024~15000**
# （Windows 默认是 49152~65535）。Hyper-V/WSL 的 NAT 服务（服务名 winnat，配套 hns /
# vmcompute）会从动态范围里按 100 个一组预留端口，当时预留了 8014-8113（正好圈住 8111）
# 和 8114-8213（正好圈住 8188）。这些预留**每次开机重新分配**，所以会出现
# 「昨天还能用、今天突然绑不上」，历史上本项目还因此从 8011 搬到过 8111。
#
# 修法两步：
#   ① 把动态端口范围改回 Windows 默认的 49152~65535 —— NAT 之后只会在高段预留，
#      不会再抢 8111/8188 这类常用低端口；
#   ② 重启 winnat 服务，让它按新范围重新预留（同时释放当前占着 8014-8213 的那批）。
#
# 用法：
#   普通权限： scripts\fix-reserved-ports.ps1 -Check    # 只诊断，不改动
#   管理员：   scripts\fix-reserved-ports.ps1            # 执行修复（或双击根目录的
#                                                       # 「修复端口启动失败.bat」，它会自动提权）
#
# 回滚：netsh int ipv4 set dynamic tcp start=1024 num=13977  再 net stop/start winnat

[CmdletBinding()]
param(
    [switch]$Check,
    # 修复后仍想保持 1024 起的动态范围时用：只重启 winnat 重新洗牌（不保证长效）
    [switch]$ReshuffleOnly
)

$ErrorActionPreference = 'Stop'

# 需要能绑定的端口：8111 = 工作台后端，8188 = ComfyUI
$watchPorts = @(8111, 8188)
$defaultStart = 49152
$defaultNum = 16384

function Test-PortBind {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return @{ Port = $Port; Ok = $true; ErrorCode = 0 }
    } catch {
        $code = -1
        $exception = $_.Exception
        while ($exception) {
            if ($exception -is [System.Net.Sockets.SocketException]) { $code = [int]$exception.ErrorCode; break }
            $exception = $exception.InnerException
        }
        return @{ Port = $Port; Ok = $false; ErrorCode = $code }
    } finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

function Get-PortReport {
    $report = @{}
    foreach ($port in $watchPorts) { $report[$port] = Test-PortBind -Port $port }
    return $report
}

function Format-PortLine {
    param($Probe)
    if ($Probe.Ok) { return ('  {0}：可绑定 ✔' -f $Probe.Port) }
    switch ($Probe.ErrorCode) {
        10013 { return ('  {0}：被系统保留（winerror 10013）✘' -f $Probe.Port) }
        10048 { return ('  {0}：被其它程序占用（winerror 10048）✘' -f $Probe.Port) }
        default { return ('  {0}：无法绑定（winerror {1}）✘' -f $Probe.Port, $Probe.ErrorCode) }
    }
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host ''
Write-Host '===== H3 工作台：端口保留区间诊断 ====='
Write-Host ''
Write-Host '当前动态端口范围：'
netsh int ipv4 show dynamicport tcp
Write-Host '当前排除（保留）区间：'
netsh interface ipv4 show excludedportrange protocol=tcp
Write-Host ''

$before = Get-PortReport
Write-Host '关键端口占用情况：'
foreach ($port in $watchPorts) { Write-Host (Format-PortLine -Probe $before[$port]) }
Write-Host ''

$blocked = @($watchPorts | Where-Object { -not $before[$_].Ok })
if ($blocked.Count -eq 0) {
    Write-Host '结论：8111 与 8188 都能正常绑定，无需修复。' -ForegroundColor Green
    exit 0
}

Write-Host ('结论：端口 ' + ($blocked -join '、') + ' 当前无法绑定。') -ForegroundColor Yellow
if ($before[$blocked[0]].ErrorCode -eq 10048) {
    Write-Host '提示：winerror 10048 是真的被别的程序占用，请先用 netstat -ano | findstr :<端口> 找到进程。' -ForegroundColor Yellow
}
if ($before[$blocked[0]].ErrorCode -eq 10013) {
    Write-Host '提示：winerror 10013 表示端口落在系统保留区间里（本机由 Hyper-V/WSL 的 NAT 预留）。' -ForegroundColor Yellow
}

if ($Check) {
    Write-Host ''
    Write-Host '（-Check 模式只诊断，未做任何改动）' -ForegroundColor DarkGray
    exit 1
}

if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host '修复需要管理员权限。' -ForegroundColor Red
    Write-Host '请双击项目根目录的「修复端口启动失败.bat」（会自动弹 UAC），' -ForegroundColor Yellow
    Write-Host '或在管理员 PowerShell 里运行：' -ForegroundColor Yellow
    Write-Host ('  powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"') -ForegroundColor Yellow
    exit 1
}

Write-Host ''
Write-Host '===== 开始修复 ====='
Write-Host '（会短暂重启 winnat 服务：WSL2 / Docker 的网络会中断几秒，请先停掉相关容器）' -ForegroundColor DarkGray
Write-Host ''

if ($ReshuffleOnly) {
    Write-Host '模式：只重启 winnat 重新洗牌（不改动态端口范围）'
} else {
    Write-Host ('步骤一：把动态端口范围改回 Windows 默认（{0} 起、{1} 个）' -f $defaultStart, $defaultNum)
    Write-Host '        原值可用这条恢复：netsh int ipv4 set dynamic tcp start=1024 num=13977' -ForegroundColor DarkGray
}

Write-Host '步骤二：停止 winnat ……'
& net stop winnat
if ($LASTEXITCODE -ne 0) {
    Write-Host '停止 winnat 失败：可能有依赖它的组件（WSL2 / Docker / 沙盒）在跑。' -ForegroundColor Red
    Write-Host '先关掉它们；仍不行可再试 net stop hns（容器网络服务），' -ForegroundColor Yellow
    Write-Host '或者直接重启一次电脑（重启也会重新分配保留区间）。' -ForegroundColor Yellow
    exit 1
}

if (-not $ReshuffleOnly) {
    & netsh int ipv4 set dynamic tcp ("start={0}" -f $defaultStart) ("num={0}" -f $defaultNum) | Out-Null
}

Write-Host '步骤三：启动 winnat ……'
& net start winnat
if ($LASTEXITCODE -ne 0) {
    Write-Host '启动 winnat 失败，请重启电脑恢复网络后重试。' -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host '修复后的端口情况：'
$after = Get-PortReport
foreach ($port in $watchPorts) { Write-Host (Format-PortLine -Probe $after[$port]) }
Write-Host ''
Write-Host '修复后的排除区间（应只剩高段）：'
netsh interface ipv4 show excludedportrange protocol=tcp
Write-Host ''

$stillBlocked = @($watchPorts | Where-Object { -not $after[$_].Ok })
if ($stillBlocked.Count -eq 0) {
    Write-Host '修复成功：8111 与 8188 都能绑定了。' -ForegroundColor Green
    Write-Host '下一步：双击「启动H3影动高清工作台.bat」启动工作台。' -ForegroundColor Green
    exit 0
}

Write-Host ('仍有端口无法绑定：' + ($stillBlocked -join '、')) -ForegroundColor Red
Write-Host '如果错误码仍是 10013，请重启一次电脑（重启会重新分配保留区间）。' -ForegroundColor Yellow
Write-Host '如果变成 10048，说明是真被别的程序占用了，用 netstat -ano | findstr :<端口> 查。' -ForegroundColor Yellow
exit 1
