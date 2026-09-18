# H3 chat-mode image generation, on demand (resident loop).
#
# User's flow (2026-09-19): only when the batch has nothing runnable left, and only for
# THE ONE work that is stuck waiting for its person image -- then generate just that one.
#   .\scripts\run-chat-imagegen-need.ps1              # start (default: check every 300s)
#   .\scripts\run-chat-imagegen-need.ps1 -Status      # is it running? last log lines
#   .\scripts\run-chat-imagegen-need.ps1 -Stop        # stop it
#
# ASCII only on purpose: a .ps1 containing CJK gets mis-decoded by this machine's
# PowerShell 5.1 without a UTF-8 BOM (mojibake eats string terminators).
param(
    [double]$Loop = 300,
    [double]$CopyWait = 180,
    [double]$Cooldown = 30,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entry = Join-Path $projectRoot 'scripts\chat_imagegen.py'
$logDir = Join-Path $projectRoot 'data'
$stdout = Join-Path $logDir 'chat-imagegen-need-stdout.log'
$stderr = Join-Path $logDir 'chat-imagegen-need-stderr.log'
$pidFile = Join-Path $logDir 'chat-imagegen-need.pid'

function Get-Daemon {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return $null }
    $procId = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$procId)) { return $null }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    # The pid file may be stale and the id reused by an unrelated process: check the
    # command line still looks like our entry script.
    $info = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if ($info -and $info.CommandLine -and ($info.CommandLine -notmatch 'chat_imagegen')) { return $null }
    return $proc
}

if ($Stop) {
    $proc = Get-Daemon
    if (-not $proc) { Write-Host 'not running'; exit 0 }
    Stop-Process -Id $proc.Id -Force
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host ("stopped pid={0}" -f $proc.Id)
    exit 0
}

if ($Status) {
    $proc = Get-Daemon
    if ($proc) { Write-Host ("running pid={0}" -f $proc.Id) } else { Write-Host 'not running' }
    if (Test-Path -LiteralPath $stdout) {
        Write-Host ("--- {0} (tail) ---" -f $stdout)
        Get-Content -LiteralPath $stdout -Tail 15
    }
    if (Test-Path -LiteralPath $stderr) {
        $tail = Get-Content -LiteralPath $stderr -Tail 5
        if ($tail) { Write-Host ("--- {0} (tail) ---" -f $stderr); $tail }
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }
if (-not (Test-Path -LiteralPath $entry)) { throw "Entry script not found: $entry" }

$existing = Get-Daemon
if ($existing) {
    Write-Host ("already running pid={0} (use -Stop first)" -f $existing.Id)
    exit 1
}

# -u so the log shows progress while it waits (python buffers stdout when redirected).
$pyArgs = @(
    '-u', $entry, '--need-one', '--loop', $Loop,
    '--copy-wait', $CopyWait, '--cooldown', $Cooldown
)
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
Remove-Item -LiteralPath $stdout -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stderr -Force -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath $python -ArgumentList $pyArgs -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii

Write-Host ("started pid={0} loop={1}s" -f $proc.Id, $Loop)
Write-Host ("stdout={0}" -f $stdout)
Write-Host ("stderr={0}" -f $stderr)
