@echo off
echo Installing the Python packages GENESYS 20 Live needs (pyserial, numpy, matplotlib).
echo This needs internet.
echo.
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python was not found on this computer.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH" in the installer^), then run this file again.
  echo.
  pause
  exit /b 1
)
%PY% -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo Installing the packages failed - see the messages above.
  pause
  exit /b 1
)
echo.
echo Done. Start "GENESYS 20 Live.bat" (or the demo version to try it without the instrument).
pause
