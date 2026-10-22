@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ============================================
echo   Hy-Memory MCP 服务控制台
echo ============================================
echo   1. 启动服务
echo   2. 停止服务
echo   3. 查看状态
echo   4. 查看最近日志
echo   5. 打开配置界面
echo   6. 冒烟测试
echo   0. 退出
echo ============================================
set /p CHOICE=请选择 [0-6]: 

if "%CHOICE%"=="1" goto START
if "%CHOICE%"=="2" goto STOP
if "%CHOICE%"=="3" goto STATUS
if "%CHOICE%"=="4" goto LOGS
if "%CHOICE%"=="5" goto CONFIG
if "%CHOICE%"=="6" goto TEST
goto END

:START
cd /d "%~dp0"
netstat -ano | findstr ":19527" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [i] 服务已在运行
    goto END
)
echo 启动中...
wscript.exe "%~dp0start_server.vbs"
timeout /t 6 >nul
netstat -ano | findstr ":19527" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo [X] 启动失败，请查看日志
) else (
    echo [OK] 服务已启动 http://127.0.0.1:19527/mcp
)
goto END

:STOP
call "%~dp0stop_server.bat"
goto END

:STATUS
echo.
echo --- 端口 19527 ---
netstat -ano | findstr ":19527" | findstr "LISTENING" || echo   未监听（服务已停止）
echo.
echo --- 计划任务 ---
schtasks /Query /TN "HyMemoryMCP" /FO LIST 2>nul | findstr /R "Status Last" || echo   未注册开机自启
goto END

:LOGS
if exist "%~dp0logs\server.log" (
    powershell -NoProfile -Command "Get-Content '%~dp0logs\server.log' -Tail 40 -Encoding UTF8"
) else (
    echo   还没有日志文件（服务尚未通过新版启动）
)
goto END

:CONFIG
start "" "http://127.0.0.1:19528"
echo 已在浏览器打开（需先启动配置界面 open_config.bat 或由任务计划托管）
goto END

:TEST
cd /d "%~dp0"
echo 运行冒烟测试（需服务已启动）...
".venv\Scripts\python.exe" test_mcp.py
goto END

:END
echo.
pause
