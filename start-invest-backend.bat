@echo off
chcp 65001 >nul 2>&1
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Load local .env before choosing Python/uv so double-click startup can use
rem OPENINVEST_PYTHON / OPENINVEST_UV without manual system environment setup.
set "SCRIPT_DIR=%~dp0"
set "ENV_FILE=%SCRIPT_DIR%.env"
if exist "%ENV_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        echo(%%A| findstr /r "^[A-Za-z_][A-Za-z0-9_]*$" >nul && if not defined %%A set "%%A=%%B"
    )
)
if defined OPENINVEST_ROOT set "OPENINVEST_ROOT=%OPENINVEST_ROOT:"=%"
if defined OPENINVEST_PYTHON set "OPENINVEST_PYTHON=%OPENINVEST_PYTHON:"=%"
if defined OPENINVEST_UV set "OPENINVEST_UV=%OPENINVEST_UV:"=%"

rem Portable double-click entrypoint. The real startup logic lives in
rem scripts\start_invest_backend.py to avoid fragile batch quoting.
if "%OPENINVEST_ROOT%"=="" set "OPENINVEST_ROOT=%SCRIPT_DIR%"
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
