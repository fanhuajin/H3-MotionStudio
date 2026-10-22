$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logDir = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$progressFile = Join-Path ([System.IO.Path]::GetTempPath()) ('h3-start-' + [guid]::NewGuid() + '.txt')
$env:H3_STARTUP_PROGRESS = $progressFile
$form = New-Object System.Windows.Forms.Form
$form.Text = 'H3 影动高清工作台'
$form.ClientSize = New-Object System.Drawing.Size(560, 290)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'None'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ControlBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(25, 34, 32)
$form.ForeColor = [System.Drawing.Color]::FromArgb(222, 232, 226)
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
# 自绘背景开启双缓冲，避免呼吸动画刷新时闪烁。
$bufferProperty = [System.Windows.Forms.Form].GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'Instance,NonPublic')
$bufferProperty.SetValue($form, $true, $null)
$shape = New-Object System.Drawing.Drawing2D.GraphicsPath
$shape.AddArc(0, 0, 24, 24, 180, 90)
$shape.AddArc(535, 0, 24, 24, 270, 90)
$shape.AddArc(535, 265, 24, 24, 0, 90)
$shape.AddArc(0, 265, 24, 24, 90, 90)
$shape.CloseFigure()
$form.Region = [System.Drawing.Region]::new($shape)
$shape.Dispose()
$script:progressValue = 5
$script:pulse = 0
$script:ready = $false
$script:fadeTicks = 0
$appIcon = [System.Drawing.Icon]::new((Join-Path $projectRoot 'assets\H3-MotionStudio.ico'), 256, 256)
# 现有 ICO 内嵌 PNG；直接读取该帧，避开 Windows PowerShell 的 ToBitmap 兼容问题。
$iconBytes = [IO.File]::ReadAllBytes((Join-Path $projectRoot 'assets\H3-MotionStudio.ico'))
$imageOffset = [BitConverter]::ToUInt32($iconBytes, 18)
$imageLength = [BitConverter]::ToUInt32($iconBytes, 14)
$iconStream = [IO.MemoryStream]::new($iconBytes, $imageOffset, $imageLength)
$iconFrame = [System.Drawing.Image]::FromStream($iconStream)
$logoImage = [System.Drawing.Bitmap]::new($iconFrame)
$iconFrame.Dispose()
$iconStream.Dispose()
$form.Icon = $appIcon
$form.Add_Paint({
    param($sender, $eventArgs)
    $g = $eventArgs.Graphics
    $g.SmoothingMode = 'AntiAlias'
    $background = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        $form.ClientRectangle, [System.Drawing.ColorTranslator]::FromHtml('#20392E'),
        [System.Drawing.ColorTranslator]::FromHtml('#101A18'), 30)
    $g.FillRectangle($background, $form.ClientRectangle)
    $background.Dispose()
    $glowPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $glowPath.AddEllipse(300, -130, 360, 340)
    $glow = [System.Drawing.Drawing2D.PathGradientBrush]::new($glowPath)
    $glow.CenterColor = [System.Drawing.Color]::FromArgb(45, 126, 192, 140)
    $glow.SurroundColors = @([System.Drawing.Color]::Transparent)
    $g.FillPath($glow, $glowPath)
    $glow.Dispose(); $glowPath.Dispose()
    $iconHaloPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $iconHaloPath.AddEllipse(12, 68, 112, 112)
    $iconHalo = [System.Drawing.Drawing2D.PathGradientBrush]::new($iconHaloPath)
    $iconHalo.CenterColor = [System.Drawing.Color]::FromArgb(65, 109, 189, 176)
    $iconHalo.SurroundColors = @([System.Drawing.Color]::Transparent)
    $g.FillPath($iconHalo, $iconHaloPath)
    $iconHalo.Dispose(); $iconHaloPath.Dispose()
    $g.InterpolationMode = 'HighQualityBicubic'
    $g.DrawImage($logoImage, 32, 89, 72, 72)
    $track = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#30473A'))
    $g.FillRectangle($track, 36, 224, 488, 4)
    $track.Dispose()
    $fill = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#ABD8AE'))
    $g.FillRectangle($fill, 36, 224, [int](488 * $script:progressValue / 100), 4)
    $fill.Dispose()
    $dot = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(
        [int](130 + 100 * [Math]::Sin($script:pulse)), 171, 216, 174))
    $g.FillEllipse($dot, 514, 38, 6, 6)
    $dot.Dispose()
})
function Add-StartupLabel($Text, $X, $Y, $Size, $Color) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $Text
    $label.Location = [System.Drawing.Point]::new($X, $Y)
    $label.AutoSize = $true
    $label.BackColor = [System.Drawing.Color]::Transparent
    $label.ForeColor = [System.Drawing.ColorTranslator]::FromHtml($Color)
    $label.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', $Size)
    $form.Controls.Add($label)
    return $label
}
$brand = Add-StartupLabel 'H3 / MOTION STUDIO' 34 32 8 '#93B5A0'
$subtitle = Add-StartupLabel '让灵感，成为画面' 119 136 9 '#91AD9A'
$footer = Add-StartupLabel '正在为你准备创作空间' 34 245 8 '#6F8F7B'
$title = New-Object System.Windows.Forms.Label
$title.Text = '影动高清工作台'
$title.Location = New-Object System.Drawing.Point(116, 96)
$title.BackColor = [System.Drawing.Color]::Transparent
$title.AutoSize = $true
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 20)
$status = New-Object System.Windows.Forms.Label
$status.Text = '正在启动…'
$status.Location = New-Object System.Drawing.Point(34, 192)
$status.BackColor = [System.Drawing.Color]::Transparent
$status.ForeColor = [System.Drawing.ColorTranslator]::FromHtml('#BDD0C2')
$status.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 9)
$status.Size = New-Object System.Drawing.Size(400, 25)
$form.Controls.AddRange(@($title, $status))
# 只绘制设计预览，不启动服务或打开浏览器。
if ($env:H3_STARTUP_PREVIEW_PATH) {
    $bitmap = [System.Drawing.Bitmap]::new(560, 290)
    try {
        $form.Show()
        [System.Windows.Forms.Application]::DoEvents()
        $form.DrawToBitmap($bitmap, $form.ClientRectangle)
        $bitmap.Save($env:H3_STARTUP_PREVIEW_PATH, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $bitmap.Dispose()
        $form.Close()
        $form.Dispose()
        $logoImage.Dispose()
        $appIcon.Dispose()
        Remove-Item Env:\H3_STARTUP_PROGRESS -ErrorAction SilentlyContinue
    }
    return
}
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 40
$script:worker = $null
$timer.Add_Tick({
    try {
        $script:pulse += 0.10
        if ($script:ready) {
            $script:fadeTicks += 1
            $form.Opacity = [Math]::Max(0.01, 1.0 - $script:fadeTicks / 10.0)
            if ($script:fadeTicks -ge 10) { $timer.Stop(); $form.Close() }
            return
        }
        if (Test-Path -LiteralPath $progressFile) {
            $parts = [System.IO.File]::ReadAllText($progressFile).Split('|', 2)
            if ($parts.Count -eq 2) {
                $script:progressValue = [Math]::Min(100, [Math]::Max(0, [int]$parts[0]))
                $status.Text = $parts[1]
                if ($script:progressValue -eq 100) { $script:ready = $true }
            }
        }
        if ($script:worker -and $script:worker.HasExited) {
            if ($script:ready) { return }
            throw '启动未完成。请查看 data\startup-stderr.log 和 data\startup-stdout.log。'
        }
        $form.Invalidate()
    } catch {
        $timer.Stop()
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '工作台启动失败', 'OK', 'Error') | Out-Null
        $form.Close()
    }
})
$form.Add_Shown({
    try {
        $script:worker = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $PSScriptRoot 'start-local.ps1') + '"')
        ) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'startup-stdout.log') `
            -RedirectStandardError (Join-Path $logDir 'startup-stderr.log')
        $timer.Start()
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '工作台启动失败', 'OK', 'Error') | Out-Null
        $form.Close()
    }
})
try { [void]$form.ShowDialog() } finally {
    $timer.Dispose()
    $form.Dispose()
    $logoImage.Dispose()
    $appIcon.Dispose()
    Remove-Item -LiteralPath $progressFile -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\H3_STARTUP_PROGRESS -ErrorAction SilentlyContinue
}
