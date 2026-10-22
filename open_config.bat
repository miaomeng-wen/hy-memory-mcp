@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 检查服务端口 19527 ...
netstat -ano | findstr ":19527" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo   端口未监听，启动中...
    wscript.exe "%~dp0start_server.vbs"
    timeout /t 5 >nul
) else (
    echo   已在运行，跳过启动
)

echo 打开配置界面 http://127.0.0.1:19528 ...
start "" "http://127.0.0.1:19528"

echo.
echo 确认服务状态：
netstat -ano | findstr ":19527" | findstr "LISTENING" || echo   [警告] 服务未监听
