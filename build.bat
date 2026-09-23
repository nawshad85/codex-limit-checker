@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

"%PYTHON_EXE%" --version >nul 2>nul
if errorlevel 1 goto :python_missing

"%PYTHON_EXE%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 goto :pyinstaller_missing

echo Building Codex Usage Monitor...
"%PYTHON_EXE%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "CodexUsageMonitor" ^
    --add-data "assets\openai-white-monoblossom.png;assets" ^
    "main.py"

set "BUILD_EXIT=%ERRORLEVEL%"
if not "%BUILD_EXIT%"=="0" goto :build_failed
if not exist "dist\CodexUsageMonitor.exe" goto :output_missing

echo.
echo Build complete: %CD%\dist\CodexUsageMonitor.exe
exit /b 0

:python_missing
echo ERROR: Python was not found.
echo Create .venv as described in README.md, or add Python to PATH.
exit /b 1

:pyinstaller_missing
echo ERROR: PyInstaller is not installed in the selected Python environment.
echo Run: "%PYTHON_EXE%" -m pip install -r requirements-build.txt
exit /b 1

:build_failed
echo.
echo ERROR: PyInstaller failed with exit code %BUILD_EXIT%.
exit /b %BUILD_EXIT%

:output_missing
echo ERROR: PyInstaller finished but dist\CodexUsageMonitor.exe was not found.
exit /b 1
