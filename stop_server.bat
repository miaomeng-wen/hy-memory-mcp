@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

set PORT=19527
echo 正在查找监听 %PORT% 端口的进程...

set FOUND=0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo   结束 PID %%P
    taskkill /PID %%P /F >nul 2>&1
    if errorlevel 1 (
        echo   [失败] 无法结束 PID %%P（可能需要管理员权限）
    ) else (
        set FOUND=1
        echo   [成功] 已结束 PID %%P
    )
)

if "!FOUND!"=="0" (
    echo 没有找到监听 %PORT% 的进程，服务可能没在运行。
)

echo.
timeout /t 2 >nul
echo 当前端口状态：
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" || echo   端口已释放
