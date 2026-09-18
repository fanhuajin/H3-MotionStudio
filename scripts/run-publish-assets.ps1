# H3 发布素材生成后台启动器
param(
    [string[]]$Folders = @(),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entry = Join-Path $projectRoot 'scripts\make_publish_assets_via_cdp.py'
$logDir = Join-Path $projectRoot 'data'
$stdout = Join-Path $logDir 'publish-assets-stdout.log'
$stderr = Join-Path $logDir 'publish-assets-stderr.log'
$pidFile = Join-Path $logDir 'publish-assets.pid'

if (-not (Test-Path -LiteralPath $python)) { throw "找不到 Python：$python" }
if (-not (Test-Path -LiteralPath $entry)) { throw "找不到脚本：$entry" }

$args = @($entry)
if ($Folders.Count -gt 0) {
    $args += '--folders'
    $args += $Folders
}
if ($Force) { $args += '--force' }

Remove-Item -LiteralPath $stdout -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stderr -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue

$proc = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $projectRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii

Write-Host ("started pid={0}" -f $proc.Id)
Write-Host ("stdout={0}" -f $stdout)
Write-Host ("stderr={0}" -f $stderr)
