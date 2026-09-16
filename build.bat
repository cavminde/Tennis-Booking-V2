@echo off
chcp 65001 >nul
REM ============================================================
REM  海大网球订场 V2 · 打包脚本
REM  源码：src\    产物：dist\HainanU_Tennis_Booking_V2.exe
REM ============================================================
cd /d "%~dp0"

set PY=C:\Users\Cavminde\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [1/4] 检查依赖...
"%PY%" -c "import requests, PyInstaller" 2>nul
if errorlevel 1 (
    echo       正在安装依赖...
    "%PY%" -m pip install requests pyinstaller
    if errorlevel 1 (
        echo [失败] 依赖安装失败。
        pause & exit /b 1
    )
)

echo [2/4] 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist HainanU_Tennis_Booking_V2.spec del /q HainanU_Tennis_Booking_V2.spec

echo [3/4] 打包中（约 1-2 分钟）...
pushd src
"%PY%" -m PyInstaller --clean --noconfirm --onefile --console ^
    --name HainanU_Tennis_Booking_V2 ^
    --distpath "..\dist" --workpath "..\build" --specpath ".." ^
    --add-data "index.html;." ^
    --exclude-module tkinter --exclude-module unittest ^
    --exclude-module pydoc --exclude-module pydoc_data ^
    app_server.py
set BUILD_ERR=%errorlevel%
popd

if not "%BUILD_ERR%"=="0" (
    echo [失败] 打包出错，请看上面的日志。
    pause & exit /b 1
)

echo [4/4] 复制配置...
if exist src\config.json copy /y src\config.json dist\config.json >nul

echo.
echo [完成] 产物：dist\HainanU_Tennis_Booking_V2.exe
echo      双击即启动本地服务并自动打开浏览器（端口 8081）。
pause
