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
$form.ClientSize = New-Object System.Drawing.Size(460, 180)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ControlBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(25, 34, 32)
$form.ForeColor = [System.Drawing.Color]::FromArgb(222, 232, 226)
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$title = New-Object System.Windows.Forms.Label
$title.Text = 'H3 影动高清工作台'
$title.Location = New-Object System.Drawing.Point(28, 25)
$title.AutoSize = $true
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 16)
$status = New-Object System.Windows.Forms.Label
$status.Text = '正在启动…'
$status.Location = New-Object System.Drawing.Point(30, 76)
$status.Size = New-Object System.Drawing.Size(400, 25)
$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(30, 115)
$bar.Size = New-Object System.Drawing.Size(400, 8)
$form.Controls.AddRange(@($title, $status, $bar))
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 200
$script:worker = $null
$timer.Add_Tick({
    try {
        if (Test-Path -LiteralPath $progressFile) {
            $parts = [System.IO.File]::ReadAllText($progressFile).Split('|', 2)
            if ($parts.Count -eq 2) {
                $bar.Value = [int]$parts[0]
                $status.Text = $parts[1]
                if ($bar.Value -eq 100) { $form.Close(); return }
            }
        }
        if ($script:worker -and $script:worker.HasExited) {
            throw '启动未完成。请查看 data\startup-stderr.log 和 data\startup-stdout.log。'
        }
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
    Remove-Item -LiteralPath $progressFile -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\H3_STARTUP_PROGRESS -ErrorAction SilentlyContinue
}
