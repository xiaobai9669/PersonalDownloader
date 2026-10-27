@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先运行“安装媒体支持.bat”。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --windowed --name PersonalDownloader --collect-all yt_dlp --collect-all imageio_ffmpeg --collect-all PIL --collect-all pystray downloader.py
if errorlevel 1 goto failed
echo.
echo 打包完成：dist\PersonalDownloader\PersonalDownloader.exe
echo 请将整个 dist\PersonalDownloader 文件夹复制到其他 Windows 电脑运行。
pause
exit /b 0
:failed
echo.
echo 打包失败，请查看上方错误信息。
pause
exit /b 1
