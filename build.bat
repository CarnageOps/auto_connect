@echo off
cd /d "%~dp0"
echo ============================================
echo  Building Auto-Connect  +  Network Fix
echo ============================================
echo.

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
)

echo.
echo [1/2] Building AutoConnect...
python -m PyInstaller auto_connect.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo *** AutoConnect build FAILED. ***
    echo If the error is "Access is denied", close AutoConnect.exe and retry.
    echo.
    pause
    exit /b 1
)

echo.
echo [2/2] Building NetworkFix...
python -m PyInstaller network_fix.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo *** NetworkFix build FAILED. ***
    echo If the error is "Access is denied", close NetworkFix.exe and retry.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================

set ok=1
if exist dist\AutoConnect.exe (
    echo   AutoConnect.exe  — OK
) else (
    echo   AutoConnect.exe  — MISSING
    set ok=0
)
if exist dist\NetworkFix.exe (
    echo   NetworkFix.exe   — OK
) else (
    echo   NetworkFix.exe   — MISSING
    set ok=0
)

echo ============================================
if "%ok%"=="1" (
    echo Build successful!
) else (
    echo One or more outputs missing. Check the output above.
)

echo.
pause
