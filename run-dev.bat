@echo off
setlocal
cd /d "%~dp0"

REM Always launch the live source tree in developer mode (never stale dist\SayInk\SayInk.exe).
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
    goto :after_run
)

where python >nul 2>nul
if not errorlevel 1 (
    python -c "import PyQt6" >nul 2>nul
    if not errorlevel 1 (
        python run.py %*
        goto :after_run
    )
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3.10 -c "import PyQt6" >nul 2>nul
    if not errorlevel 1 (
        py -3.10 run.py %*
        goto :after_run
    )
    py -3 -c "import PyQt6" >nul 2>nul
    if not errorlevel 1 (
        py -3 run.py %*
        goto :after_run
    )
)

echo.
echo 未找到已安装 PyQt6 的 Python 环境（已依次检查 .venv、PATH 中的 python、py -3.10、py -3）。
echo 请先安装依赖，例如: python -m pip install -r requirements.txt
pause
exit /b 1

:after_run
if errorlevel 1 (
    echo.
    echo 启动失败。可尝试: python -m pip install -r requirements.txt
    pause
    exit /b 1
)
