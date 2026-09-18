@echo off
rem NOTE: keep this file ASCII-only -- a .bat containing CJK text gets mis-decoded
rem by cmd.exe on this machine (codepage 936) and the mojibake breaks the script.
rem Fixes 8111 / 8188 being inside a Windows *reserved* port range (winerror 10013).
rem No goto/labels on purpose: labels plus LF-only line endings are a known cmd trap.
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel%==0 (
  where pwsh.exe >nul 2>nul
  if %errorlevel%==0 (
    pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\fix-reserved-ports.ps1"
  ) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\fix-reserved-ports.ps1"
  )
) else (
  echo Requesting administrator rights ^(UAC prompt^)...
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
pause
