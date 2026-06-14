@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Portable double-click entrypoint. The real startup logic lives in
rem scripts\start_invest_backend.py to avoid fragile batch quoting.
if "%OPENINVEST_ROOT%"=="" set "OPENINVEST_ROOT=%~dp0"
for %%I in ("%OPENINVEST_ROOT%\.") do set "OPENINVEST_ROOT=%%~fI"
cd /d "%OPENINVEST_ROOT%" || exit /b 1

if not "%OPENINVEST_PYTHON%"=="" (
    set "PYTHON_CMD=%OPENINVEST_PYTHON%"
) else if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
) else (
    set "PYTHON_CMD="
)

if not "%PYTHON_CMD%"=="" (
    "%PYTHON_CMD%" -m scripts.start_invest_backend %*
) else (
    if "%OPENINVEST_UV%"=="" set "OPENINVEST_UV=uv"
    "%OPENINVEST_UV%" run python -m scripts.start_invest_backend %*
)

if errorlevel 1 (
    echo.
    echo OpenInvest startup failed. Check the message above and logs\*.err.log.
    pause
    exit /b 1
)
