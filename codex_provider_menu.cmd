@echo off
setlocal
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%codex_provider_menu.py"

if not exist "%PY_SCRIPT%" (
  echo codex_provider_menu.py not found next to this .cmd file.
  echo Expected: "%PY_SCRIPT%"
  pause
  exit /b 1
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 "%PY_SCRIPT%"
) else (
  where python >nul 2>nul
  if %ERRORLEVEL% NEQ 0 (
    echo Python was not found. Install Python or add it to PATH.
    pause
    exit /b 1
  )
  python "%PY_SCRIPT%"
)

echo.
echo Press any key to close...
pause >nul
