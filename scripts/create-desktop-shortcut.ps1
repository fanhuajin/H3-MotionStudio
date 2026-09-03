$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$assetDirectory = Join-Path $projectRoot 'assets'
$iconPath = Join-Path $assetDirectory 'H3-MotionStudio.ico'
$launcherPath = Join-Path $projectRoot '启动H3影动高清工作台.bat'
$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktopPath 'H3影动高清工作台.lnk'

New-Item -ItemType Directory -Path $assetDirectory -Force | Out-Null

Add-Type -AssemblyName System.Drawing
$bitmap = [System.Drawing.Bitmap]::new(256, 256)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

try {
    $graphics.Clear([System.Drawing.Color]::FromArgb(10, 14, 39))
    $background = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Rectangle]::new(0, 0, 256, 256),
        [System.Drawing.Color]::FromArgb(31, 42, 101),
        [System.Drawing.Color]::FromArgb(9, 13, 36),
        45
    )
    $graphics.FillRectangle($background, 10, 10, 236, 236)
    $background.Dispose()

    $halo = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(38, 46, 201, 245))
    $graphics.FillEllipse($halo, 170, -20, 120, 120)
    $halo.Dispose()

    $font = [System.Drawing.Font]::new('Arial', 96, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $cyan = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(77, 220, 255))
    $format = [System.Drawing.StringFormat]::new()
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $graphics.DrawString('H3', $font, $cyan, [System.Drawing.RectangleF]::new(0, 42, 256, 142), $format)

    $track = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(70, 84, 159))
    $graphics.FillRectangle($track, 69, 194, 118, 12)
    $track.Dispose()
    $graphics.FillRectangle($cyan, 87, 194, 82, 12)

    $format.Dispose()
    $cyan.Dispose()
    $font.Dispose()

    $png = [System.IO.MemoryStream]::new()
    $bitmap.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
    $pngBytes = $png.ToArray()
    $png.Dispose()

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
} finally {
    $graphics.Dispose()
    $bitmap.Dispose()
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcherPath
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Description = '启动 H3 影动高清工作台'
$shortcut.Save()

Write-Output $shortcutPath
Write-Output $iconPath
