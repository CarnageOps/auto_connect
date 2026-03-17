@echo off
echo ============================================
echo  Building Auto-Connect Windows executable
echo ============================================
echo.

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
)

echo.
echo Running PyInstaller...
python -m PyInstaller auto_connect.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo *** Build FAILED. ***
    echo If the error is "Access is denied", close AutoConnect.exe and retry.
    echo.
    pause
    exit /b 1
)

echo.
if exist dist\AutoConnect.exe (
    echo Build successful!
    echo Output: dist\AutoConnect.exe
) else (
    echo Build FAILED — output binary not found. Check the output above for errors.
)

echo.
pause
