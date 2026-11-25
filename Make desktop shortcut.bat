@echo off
:: Puts a "GENESYS 20 Live" shortcut (with the app's icon) on this user's desktop.
set "G20_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d = $env:G20_DIR.TrimEnd('\');" ^
  "$desk = [Environment]::GetFolderPath('Desktop');" ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desk 'GENESYS 20 Live.lnk'));" ^
  "$s.TargetPath = Join-Path $d 'GENESYS 20 Live.bat';" ^
  "$s.WorkingDirectory = $d;" ^
  "$s.IconLocation = (Join-Path $d 'genesys20.ico') + ',0';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'Live readings and graphs from a Thermo Scientific GENESYS 20';" ^
  "$s.Save(); Write-Host ('Shortcut created: ' + $s.FullName)"
echo.
pause
