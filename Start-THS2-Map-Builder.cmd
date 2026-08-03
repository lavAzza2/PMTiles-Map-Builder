@echo off
setlocal
cd /d "%~dp0"
where pyw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pyw.exe -3 "%~dp0ths2_map_builder.pyw"
  exit /b 0
)
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%~dp0ths2_map_builder.pyw"
  exit /b 0
)
echo Python 3 was not found. Install Python 3 or build the standalone EXE.
pause
