<#
.SYNOPSIS
    注册 Hy-Memory MCP 服务为“登录时自动启动”的计划任务。

.DESCRIPTION
    创建名为 HyMemoryMCP 的计划任务：
      - 触发：当前用户登录后延迟 10 秒（等网络和磁盘就绪）
      - 操作：wscript.exe start_server.vbs（无窗口）
      - 失败重试 3 次，间隔 1 分钟

    需要管理员权限。

.EXAMPLE
    右键“以管理员身份运行 PowerShell”，然后：
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'HyMemoryMCP',
    [int]$DelaySeconds = 10
)

$ErrorActionPreference = 'Stop'
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbsPath = Join-Path $baseDir 'start_server.vbs'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host '[X] 需要管理员权限。' -ForegroundColor Red
    Write-Host '    请右键 PowerShell 选择以管理员身份运行，再执行本脚本。' -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $vbsPath)) {
    Write-Host "[X] 找不到启动脚本: $vbsPath" -ForegroundColor Red
    exit 1
}

$pythonExe = Join-Path $baseDir '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonExe)) {
    Write-Host "[X] 找不到 Python 环境: $pythonExe" -ForegroundColor Red
    exit 1
}

$envFile = Join-Path $baseDir '.env'
if (-not (Test-Path $envFile)) {
    Write-Host '[!] 警告：.env 不存在，服务会以默认配置启动（必然失败）。' -ForegroundColor Yellow
    Write-Host '    请先运行 open_config.bat 完成配置。' -ForegroundColor Yellow
}

Write-Host "任务名   : $TaskName"
Write-Host "启动脚本 : $vbsPath"
Write-Host "延迟     : $DelaySeconds 秒，用户登录后触发"
Write-Host ''

# 已存在则先删除，保证幂等
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host '[i] 已存在同名任务，先移除旧定义...'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# 操作：wscript.exe 以隐藏方式运行 vbs
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbsPath`"" -WorkingDirectory $baseDir

# 触发器：当前用户登录时，延迟等待
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT${DelaySeconds}S"

# 设置：失败重试、不因电池停止
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew

# 以当前用户身份运行（能读到用户目录下的 .env 与 data）
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Hy-Memory MCP 服务：为所有 AI 智能体提供共享长期记忆 (http://127.0.0.1:19527/mcp)' `
    | Out-Null

Write-Host '[OK] 计划任务已注册。' -ForegroundColor Green
Write-Host ''
Write-Host '现在测试启动一次...'
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8

$listening = Get-NetTCPConnection -LocalPort 19527 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host '[OK] 服务已在监听 19527，自启配置生效。' -ForegroundColor Green
} else {
    Write-Host '[!] 端口 19527 未监听。日志如下：' -ForegroundColor Yellow
    $logFile = Join-Path $baseDir 'logs\server.log'
    if (Test-Path $logFile) {
        Get-Content $logFile -Tail 20 -Encoding UTF8
    } else {
        Write-Host '    日志文件尚未生成，可能是 Python 环境或 .env 有问题。'
    }
}

Write-Host ''
Write-Host '常用命令：'
Write-Host "  schtasks /Run    /TN $TaskName    # 手动启动"
Write-Host "  schtasks /End    /TN $TaskName    # 停止任务"
Write-Host "  schtasks /Query  /TN $TaskName /V # 查看详情"
