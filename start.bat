@echo off
chcp 65001 >nul 2>&1
title AI CRM System Launcher
setlocal enabledelayedexpansion

echo.
echo  ============================================
echo      AI CRM System - Quick Start Launcher
echo  ============================================
echo.

:: 1. Check Python - auto install if not found
echo [1/5] Checking Python environment...
python --version >nul 2>&1
if !errorlevel! neq 0 (
    echo [WARN] Python not found. Starting automatic installation...
    echo.

    set "PYTHON_INSTALLER=%TEMP%\python-installer.exe"
    set "PYTHON_INSTALL_DIR=%LOCALAPPDATA%\Programs\Python\Python312"

    if exist "!PYTHON_INSTALL_DIR!\python.exe" (
        echo [INFO] Python found at !PYTHON_INSTALL_DIR!
        set "PATH=!PYTHON_INSTALL_DIR!;!PYTHON_INSTALL_DIR!\Scripts;!PATH!"
        python --version >nul 2>&1
        if !errorlevel! equ 0 goto :python_ok
    )

    echo Downloading Python 3.12 installer...
    echo This may take a few minutes depending on your network.
    echo.

    powershell -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.3/python-3.12.3-amd64.exe' -OutFile '%TEMP%\python-installer.exe' -UseBasicParsing; Write-Host '[OK] Download complete' } catch { Write-Host '[ERROR] Download failed:' $_.Exception.Message; exit 1 }"

    if !errorlevel! neq 0 (
        echo.
        echo [ERROR] Failed to download Python installer.
        echo Please check your internet connection and try again.
        echo Or install Python manually from: https://www.python.org/downloads/
        echo.
        echo IMPORTANT: Check "Add Python to PATH" during installation.
        pause
        exit /b 1
    )

    echo.
    echo Installing Python silently...
    echo Installing to: !PYTHON_INSTALL_DIR!
    echo.

    "!PYTHON_INSTALLER!" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 InstallLauncherAllUsers=0

    set "WAIT_COUNT=0"
    :wait_install
    if exist "!PYTHON_INSTALL_DIR!\python.exe" (
        echo [OK] Python installed successfully
        goto :install_done
    )
    set /a WAIT_COUNT+=1
    if !WAIT_COUNT! geq 60 (
        echo [ERROR] Python installation timed out
        echo Please install Python manually from: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    timeout /t 2 /nobreak >nul
    echo Waiting for installation to complete... !WAIT_COUNT!
    goto :wait_install

    :install_done
    del "%TEMP%\python-installer.exe" >nul 2>&1

    set "PATH=!PYTHON_INSTALL_DIR!;!PYTHON_INSTALL_DIR!\Scripts;!PATH!"

    python --version >nul 2>&1
    if !errorlevel! neq 0 (
        "!PYTHON_INSTALL_DIR!\python.exe" --version >nul 2>&1
        if !errorlevel! neq 0 (
            echo [ERROR] Python installation verification failed
            echo Please install Python manually from: https://www.python.org/downloads/
            pause
            exit /b 1
        ) else (
            echo @"!PYTHON_INSTALL_DIR!\python.exe" %%*> "!PYTHON_INSTALL_DIR!\Scripts\python.bat" 2>nul
            set "PATH=!PYTHON_INSTALL_DIR!;!PYTHON_INSTALL_DIR!\Scripts;!PATH!"
        )
    )

    python --version
    echo.
    goto :python_ok
)
python --version
echo.

:python_ok
echo [OK] Python is ready
echo.

:: 2. Enter backend directory
cd /d "%~dp0backend"

:: 3. Check .env config file
echo [2/5] Checking config file...
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo [INFO] Created .env from .env.example template
        echo.
        echo ============================================
        echo   IMPORTANT: Configure DeepSeek API Key
        echo ============================================
        echo.
        echo   A notepad window will open for you to edit the config.
        echo   Get your API Key from: https://platform.deepseek.com/
        echo.
        echo   If you don't have an API Key yet, just close notepad
        echo   and press any key. The system will run in Mock mode.
        echo.
        echo   Press any key to open config file...
        pause >nul
        start notepad .env
        echo After editing, press any key to continue...
        pause >nul
    ) else (
        echo [ERROR] .env.example template not found
        pause
        exit /b 1
    )
) else (
    echo [OK] Config file exists
)
echo.

:: 4. Install dependencies
echo [3/5] Installing Python dependencies...
python -m pip install --upgrade pip -q 2>nul
pip install -r requirements.txt
if !errorlevel! neq 0 (
    echo [WARN] Retrying with --no-cache-dir...
    pip install -r requirements.txt --no-cache-dir
    if !errorlevel! neq 0 (
        echo.
        echo [ERROR] Failed to install dependencies
        echo Please check your internet connection and try again
        echo.
        echo You can try manual install:
        echo   cd backend
        echo   pip install -r requirements.txt
        pause
        exit /b 1
    )
)
echo [OK] Dependencies installed
echo.

:: 5. Start service
echo [4/5] Starting service...
echo.
echo --------------------------------------------
echo   Service starting, please wait...
echo   URL: http://127.0.0.1:8000
echo   Press Ctrl+C to stop
echo --------------------------------------------
echo.

:: Open browser after 8 seconds (robust method)
start "" /b cmd /c "timeout /t 8 /nobreak >nul 2>&1 & start http://127.0.0.1:8000"

:: Start service (foreground)
echo [5/5] Service is running. Browser will open shortly.
echo.
echo If the browser does not open automatically,
echo please manually visit: http://127.0.0.1:8000
echo.
python app.py

:: If we reach here, the service stopped or crashed
echo.
echo ============================================
echo   Service has stopped
echo ============================================
echo.
echo If the service exited unexpectedly, check the error messages above.
echo.
echo Common issues:
echo   1. Port 8000 is in use - close other programs using this port
echo   2. Missing Python dependencies - re-run this script
echo   3. Config file error - check backend\.env
echo.
pause
