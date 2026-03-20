@echo off
cd /d "%~dp0"
echo ============================================
echo  Building NetworkFix Windows executable
echo ============================================
echo.

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
)

echo.
echo Running PyInstaller...
python -m PyInstaller network_fix.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo *** Build FAILED. ***
    echo If the error is "Access is denied", close NetworkFix.exe and retry.
    echo.
    pause
    exit /b 1
)

echo.
if exist dist\NetworkFix.exe (
    echo Build successful!
    echo Output: dist\NetworkFix.exe
) else (
    echo Build FAILED — output binary not found. Check the output above for errors.
)

echo.
pause
