@echo off
echo ============================================
echo   BubbleGum — Building EXE
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    pause
    exit /b 1
)

:: Install PyInstaller if needed
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
    echo.
)

:: Build
echo Building BubbleGum.exe...
echo.
pyinstaller --onefile --noconsole --icon=bubblegum.ico --add-data "bubblegum.html;." --add-data "bubblegum.ico;." --name BubbleGum bubblegum_app.py

echo.
if exist dist\BubbleGum.exe (
    echo ============================================
    echo   SUCCESS: dist\BubbleGum.exe
    echo ============================================
) else (
    echo BUILD FAILED — check errors above.
)
echo.
pause
