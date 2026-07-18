@echo off
setlocal

cd /d "%~dp0"

set VENV_DIR=%~dp0.venv
set VENV_PY=%VENV_DIR%\Scripts\python.exe

if not exist "%VENV_PY%" (
    echo No .venv found -- creating one ...
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        py -3 -m venv "%VENV_DIR%"
    ) else (
        python -m venv "%VENV_DIR%"
    )
    if not exist "%VENV_PY%" (
        echo [ERROR] Could not create a virtual environment. Make sure Python 3.9+ is installed and on PATH.
        pause
        exit /b 1
    )
)

echo Installing/updating dependencies (this package + the "build" extra: PyInstaller) ...
"%VENV_PY%" -m pip install --disable-pip-version-check -e ".[build]"
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo Building K2SDownloaderm.exe ...
echo (If this fails with a file-in-use error, close any running K2SDownloaderm.exe first.)
echo.
"%VENV_PY%" -m PyInstaller k2s_gui.spec --noconfirm
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Build failed. See the output above for details.
    pause
    exit /b 1
)

echo.
echo Build succeeded.
echo Executable: %~dp0dist\K2SDownloaderm\K2SDownloaderm.exe
pause
