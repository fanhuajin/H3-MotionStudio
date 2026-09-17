# 在桌面创建「H3 封面生成」快捷方式（图标是脚本自己画的，不依赖外部素材）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\create-cover-shortcut.ps1
#
# 生成物：
#   assets\H3-Cover.ico            图标
#   scripts\run-covers.ps1 的快捷方式 → 桌面\H3封面生成.lnk
# 双击后：列出每条作品封面状态 → 把缺的补齐 → 结束时可一键打开发布目录。

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$assetDirectory = Join-Path $projectRoot 'assets'
$iconPath = Join-Path $assetDirectory 'H3-Cover.ico'
$launcherPath = Join-Path $projectRoot 'scripts\run-covers.ps1'
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath 'H3封面生成.lnk'

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "找不到启动脚本：$launcherPath"
}

New-Item -ItemType Directory -Path $assetDirectory -Force | Out-Null

# ---------------- 画图标：一张 4:3 画框 + 3:4 画框 + 标题条 ----------------
Add-Type -AssemblyName System.Drawing
$size = 256
$bitmap = [System.Drawing.Bitmap]::new($size, $size)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

try {
    $graphics.Clear([System.Drawing.Color]::FromArgb(12, 16, 32))

    $background = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Rectangle]::new(0, 0, $size, $size),
        [System.Drawing.Color]::FromArgb(34, 46, 96),
        [System.Drawing.Color]::FromArgb(9, 12, 28),
        45
    )
    $graphics.FillRectangle($background, 8, 8, $size - 16, $size - 16)
    $background.Dispose()

    $cyan = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(77, 220, 255))
    $pink = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 138, 190))
    $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(240, 245, 255))
    $penCyan = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(77, 220, 255), 9)
    $penPink = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(255, 138, 190), 9)

    # 左边的 4:3 横版画框（B站）
    $graphics.DrawRectangle($penCyan, 34, 60, 116, 87)
    # 右边的 3:4 竖版画框（抖音），稍矮稍窄
    $graphics.DrawRectangle($penPink, 168, 46, 58, 78)

    # 画框里的「人物」剪影：圆头 + 肩，暗示封面里有本人
    $graphics.FillEllipse($white, 78, 78, 28, 28)
    $graphics.FillPie($white, 62, 104, 60, 46, 180, 180)
    $graphics.FillEllipse($white, 185, 62, 18, 18)
    $graphics.FillPie($white, 174, 80, 40, 32, 180, 180)

    # 底部标题条：一条实心 + 一条短，像封面上的大字
    $graphics.FillRectangle($cyan, 40, 182, 176, 18)
    $graphics.FillRectangle($pink, 40, 210, 104, 14)

    $png = [System.IO.MemoryStream]::new()
    $bitmap.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
    $pngBytes = $png.ToArray()
    $png.Dispose()

    # 手写 ICO 容器（PNG 压缩的 256x256 单帧），与项目里另一个图标脚本同一套写法
    $stream = [System.IO.File]::Open($iconPath, [System.IO.FileMode]::Create)
    $writer = [System.IO.BinaryWriter]::new($stream)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]1)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$pngBytes.Length)
        $writer.Write([uint32]22)
        $writer.Write($pngBytes)
    } finally {
        $writer.Dispose()
        $stream.Dispose()
    }

    $penCyan.Dispose(); $penPink.Dispose()
    $cyan.Dispose(); $pink.Dispose(); $white.Dispose()
} finally {
    $graphics.Dispose()
    $bitmap.Dispose()
}

# 校验图标真的是一张能解码的 ICO（写坏了的图标会让快捷方式变成白纸一张）
Add-Type -AssemblyName System.Drawing
$probe = [System.Drawing.Icon]::new($iconPath)
$iconSize = "$($probe.Width)x$($probe.Height)"
$probe.Dispose()
if ((Get-Item -LiteralPath $iconPath).Length -lt 1000) {
    throw "图标文件异常（$( (Get-Item -LiteralPath $iconPath).Length ) 字节），请删掉后重跑"
}
Write-Output "图标已生成：$iconPath（$iconSize）"

# ---------------- 建快捷方式 ----------------
# 用 powershell.exe 直接跑脚本；-NoExit 让窗口在结束后留着（不然看不到结果）
$arguments = '-NoExit -ExecutionPolicy Bypass -File "{0}"' -f $launcherPath

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Description = 'H3 封面生成：给发布目录补齐 B站 4:3 / 抖音 3:4 封面'
$shortcut.Save()

Write-Output "快捷方式：$shortcutPath"
Write-Output "图标：$iconPath"
Write-Output "启动脚本：$launcherPath"
