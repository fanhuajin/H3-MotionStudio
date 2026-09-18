# 创建“H3人物图+封面生成”桌面快捷方式
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $projectRoot 'scripts\run-publish-assets.ps1'
if (-not (Test-Path -LiteralPath $runner)) { throw "找不到启动器：$runner" }

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'H3人物图+封面生成.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($shortcutPath)
$sc.TargetPath = 'powershell.exe'
$sc.Arguments = '-NoExit -ExecutionPolicy Bypass -File "' + $runner + '"'
$sc.WorkingDirectory = $projectRoot
$sc.Description = 'H3 发布素材生成：人物图 -> B站4x3 -> 抖音3x4'
$sc.Save()

Write-Host "已创建：$shortcutPath"
