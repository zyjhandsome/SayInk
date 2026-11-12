@echo off
setlocal
cd /d "%~dp0"

REM Always launch the live source tree in developer mode (never stale dist\SayInk\SayInk.exe).
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
) else (
    where py >nul 2>nul && py -3.10 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        py -3.10 run.py %*
    ) else (
        python run.py %*
    )
)

if errorlevel 1 (
    echo.
    echo 启动失败。可尝试: py -3.10 -m pip install -r requirements.txt
    pause
    exit /b 1
)
