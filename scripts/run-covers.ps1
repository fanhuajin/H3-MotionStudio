# 封面自动生成 —— 双击桌面快捷方式时执行这个脚本。
#
# 用户 2026-09-16 定的行为：
#   **打开就直接跑，不问你**；**关掉这个窗口 = 停止**。
#
# 流程：
#   1. 打开后先自动检查每条作品的封面齐不齐（读 --plan-json，机器可读，不猜中文文案）
#   2. 全都齐了 → 提示一句「无需生成」就结束（不会白跑一遍）
#   3. 有缺的 → 直接开始补，不弹任何确认
#   4. 想停 → 直接关窗口：会留下停止标记，生成循环每张之前都检查它，停在安全位置
#
# 已有封面自动跳过，所以随手点也不会重复出图。
# 参数会原样透传给 scripts\make_covers_via_cdp.py（如 --folders 007 / --only bili）。

param(
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Passthrough,
    [switch]$PlanOnly,          # 只检查、不生成（自检启动器用）
    [switch]$NoPause            # 结束时不停留（脚本化调用用）
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

# 中文输出要显式设成 UTF-8，否则控制台里是乱码
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$publishRoot = 'E:\AI_Exports\H3-MotionStudio\发布成品'
$stopFlag = Join-Path $projectRoot 'data\cover-stop.flag'

# 关窗口/中断时立刻停：**直接终止后台的生成进程**，再留一个停止标记兜底。
# 只写标记不够快 —— 实测当前那张图还会继续生成两三分钟才轮到检查点（用户要的是「关掉就停」）。
$script:coveredPid = 0

function Stop-CoverRun {
    # 先写兜底标记，再杀进程（顺序反了的话，进程可能在标记落地前又开一张）
    try { New-Item -ItemType File -Path $stopFlag -Force | Out-Null } catch { }
    if ($script:coveredPid -gt 0) {
        try { Stop-Process -Id $script:coveredPid -Force -ErrorAction SilentlyContinue } catch { }
    }
}

$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Stop-CoverRun }
# Ctrl+C 在 PowerShell 5.1 里不走 PowerShell.Exiting，单独接一下
try { [Console]::CancelKeyPress += { param($s, $e) Stop-CoverRun } } catch { }

function Stop-Quietly {
    param([string]$Message = '')
    if ($Message) { Write-Host ''; Write-Host $Message -ForegroundColor DarkGray }
    if ($NoPause) { exit 0 }
    Write-Host ''
    $null = Read-Host '按回车关闭窗口'
    exit 0
}

Write-Host ''
Write-Host '============================================' -ForegroundColor Cyan
Write-Host '   H3 封面生成（B站 4:3 + 抖音 3:4）' -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host '没找到项目虚拟环境 .venv，改用系统 python……' -ForegroundColor Yellow
    $python = 'python'
}

$entry = Join-Path $projectRoot 'scripts\make_covers_via_cdp.py'
if (-not (Test-Path -LiteralPath $entry)) {
    Write-Host "找不到入口脚本：$entry" -ForegroundColor Red
    Stop-Quietly
}

# ---------------- 1) 先检查：到底需不需要跑 ----------------
Write-Host ''
Write-Host '正在检查是否需要生成……' -ForegroundColor DarkGray
$scanArgs = @($entry, '--plan-json') + $Passthrough
$raw = & $python @scanArgs 2>$null
$status = $null
try {
    # 只取最后一行，前面可能有日志
    $line = ($raw | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
    if ($line) { $status = $line | ConvertFrom-Json }
} catch {
    $status = $null
}

if ($null -eq $status) {
    Write-Host '检查失败：读不到作品状态。下面是明细，请按提示排查。' -ForegroundColor Yellow
    Write-Host ''
    & $python $entry --plan
    Stop-Quietly
}

if ($status.folders -eq 0) {
    Stop-Quietly '发布成品目录里没有可处理的作品（需要 人物图.png + 发布文案.txt）。'
}

if ($status.pending -eq 0) {
    Write-Host ''
    Write-Host ("检查完毕：$($status.folders) 条作品的封面都已齐全，无需生成。") -ForegroundColor Green
    Write-Host '  B站4x3.png（1448x1086）  抖音3x4.png（1086x1448）' -ForegroundColor DarkGray
    Write-Host '（无需生成；窗口可以直接关掉）' -ForegroundColor DarkGray
    if ($NoPause) { exit 0 }
    $null = Read-Host '按回车关闭窗口'
    exit 0
}

# ---------------- 2) 有缺的：直接开跑（不问）----------------
# 用户 2026-09-16：「不要问我 只要我关闭cmd窗口就代表要停止，打开就直接运行」
Write-Host ''
Write-Host ("需要生成 $($status.pending) 张封面：") -ForegroundColor Yellow
foreach ($item in $status.pendingItems) { Write-Host "  - $item" -ForegroundColor Yellow }

if ($PlanOnly) {
    Stop-Quietly '（-PlanOnly：只检查，没有生成任何东西）'
}

# ---------------- 3) 生成（已有封面仍会逐张跳过）----------------
Write-Host ''
Write-Host '---- 开始生成 ----' -ForegroundColor DarkGray
Write-Host '（全程静默操作专用实例，不抢鼠标键盘；单张约 1~3 分钟，慢的时候会自己多等一会儿）' -ForegroundColor DarkGray
Write-Host '（要停止就直接关掉这个窗口）' -ForegroundColor DarkGray
Remove-Item -LiteralPath $stopFlag -Force -ErrorAction SilentlyContinue  # 清掉上次的停止标记
# 注意别把空字符串塞进参数里（拼字符串时踩过：Python 报 unrecognized arguments）
$runArgs = @($entry) + @($Passthrough | Where-Object { $_ -and $_.Trim() -ne '' })

# 用 .NET Process 起：既能拿到 PID（关窗口时直接干掉），又能实时回显输出
# （Start-Process 走不通：-Wait 拿不到实时输出，不 -Wait 又读不到退出码）
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $python
$psi.WorkingDirectory = $projectRoot
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
# ArgumentList 让运行库自己转义（含空格/中文的路径才不会传坏）；老框架没有这个属性时退回拼字符串
if ($psi.PSObject.Properties.Name -contains 'ArgumentList') {
    foreach ($a in $runArgs) { $psi.ArgumentList.Add([string]$a) }
} else {
    $psi.Arguments = ($runArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '
}

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
$null = $proc.Start()
$script:coveredPid = $proc.Id

while (-not $proc.HasExited) {
    while (-not $proc.StandardOutput.EndOfStream) {
        $line = $proc.StandardOutput.ReadLine()
        if ($null -ne $line) { Write-Host $line }
    }
    while (-not $proc.StandardError.EndOfStream) {
        $line = $proc.StandardError.ReadLine()
        if ($null -ne $line) { Write-Host $line -ForegroundColor DarkGray }
    }
    Start-Sleep -Milliseconds 200
}
while (-not $proc.StandardOutput.EndOfStream) { Write-Host $proc.StandardOutput.ReadLine() }
while (-not $proc.StandardError.EndOfStream) { Write-Host $proc.StandardError.ReadLine() -ForegroundColor DarkGray }
$code = $proc.ExitCode
$script:coveredPid = 0

Write-Host ''
if ($code -eq 0) {
    Write-Host '全部完成。封面就在各自的发布目录里：' -ForegroundColor Green
    Write-Host "  $publishRoot\<编号_歌名_作品号>\" -ForegroundColor Green
    Write-Host '  B站4x3.png (1448x1086)   抖音3x4.png (1086x1448)' -ForegroundColor Green
    Write-Host ''
    Write-Host '（窗口可以直接关掉；下次打开会自动检查要不要跑）' -ForegroundColor DarkGray
    if ($NoPause) { exit 0 }
    $null = Read-Host '按回车关闭窗口'
} elseif ($code -eq 3) {
    Write-Host '已停止（窗口被关闭）。已经出好的封面都已落盘。' -ForegroundColor Yellow
    Write-Host '下次打开会自动检查，接着补缺的那些。' -ForegroundColor DarkGray
} else {
    Write-Host "有未完成的部分（退出码 $code），请看上面的失败清单。" -ForegroundColor Yellow
    if ($NoPause) { exit $code }
    Write-Host ''
    $null = Read-Host '按回车关闭窗口'
}
