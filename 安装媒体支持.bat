@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo.
echo 站点解析与 FFmpeg 合并组件安装完成。
pause
exit /b 0
:failed
echo.
echo 媒体支持安装失败，请检查 Python 和网络后重试。
pause
exit /b 1
