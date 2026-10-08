@echo off
cd /d "%~dp0"

REM Prefer packaged exe (taskbar shows "SayInk"). Dev mode via pythonw still may show "Python".
if exist "dist\SayInk\SayInk.exe" (
    start "" "dist\SayInk\SayInk.exe"
    exit /b 0
)

pythonw run.py
if errorlevel 1 (
    echo.
    echo 启动失败。可尝试: pip install -r requirements.txt
    pause
)
