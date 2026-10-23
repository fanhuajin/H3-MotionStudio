Option Explicit
Dim shell, files, script, protocolKey, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
' Keep the browser launch entry available after all project services stop.
protocolKey = "HKCU\Software\Classes\h3-motionstudio\"
command = """" & files.BuildPath(files.GetSpecialFolder(0), "System32\wscript.exe") & """ """ & WScript.ScriptFullName & """"
shell.RegWrite protocolKey, "URL:H3 MotionStudio", "REG_SZ"
shell.RegWrite protocolKey & "URL Protocol", "", "REG_SZ"
' No URL arguments are forwarded: this entry can only start the fixed launcher.
shell.RegWrite protocolKey & "shell\open\command\", command, "REG_SZ"
If WScript.Arguments.Count = 1 Then
    If WScript.Arguments(0) = "--register-only" Then WScript.Quit 0
End If
script = files.BuildPath(files.GetParentFolderName(WScript.ScriptFullName), "startup-window.ps1")
shell.Run "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & script & """", 0, False
