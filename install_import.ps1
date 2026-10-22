<#
.SYNOPSIS
    注册“会话导入”计划任务：每 5 分钟触发一次短时导入，跑完即退出。

.DESCRIPTION
    任务名默认 HyMemoryImport。行为：
      - 触发：当前用户登录后立即开始，之后每 5 分钟重复一次
      - 操作：.venv\Scripts\pythonw.exe run_import.py
              （不传 --incremental：脚本自己按状态决定走回填还是增量，
                历史回填没跑完时会继续回填，否则老文件永远轮不到）
              （pythonw 不弹控制台；run_import.py 只通过 MCP 触发，
               真正的写入发生在 memory_server 进程内）
      - 上一次未结束时不启动新实例（MultipleInstances IgnoreNew）
      - 单次最多 50 片 / 120 秒，每日最多 500 片（由 codex_importer.py 内部强制）

    不需要管理员权限：任务以当前用户身份注册，只在登录状态运行。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_import.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'HyMemoryImport',
    [int]$EveryMinutes = 5
)

$ErrorActionPreference = 'Stop'
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $baseDir 'run_import.py'
$pythonw = Join-Path $baseDir '.venv\Scripts\pythonw.exe'
$python = Join-Path $baseDir '.venv\Scripts\python.exe'

foreach ($p in @($script, $pythonw, $python)) {
    if (-not (Test-Path $p)) {
        Write-Host "[X] 找不到: $p" -ForegroundColor Red
        exit 1
    }
}

Write-Host "任务名   : $TaskName"
Write-Host "入口     : $script"
Write-Host "解释器   : $pythonw"
Write-Host "频率     : 每 $EveryMinutes 分钟"
Write-Host ''

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host '[i] 已存在同名任务，先移除旧定义...'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "`"$script`"" `
    -WorkingDirectory $baseDir

# 两个触发器：
#   1) 登录时触发——重启/重新登录后自动接上
#   2) 从 1 分钟后开始每 N 分钟重复，持续 10 年——登录状态下持续增量导入
# 只写 -AtLogOn 并挂 Repetition 时，StartBoundary 为空，任务会一直
# 等到下次登录才开始重复（实测 Next Run Time = N/A），所以两个都要。
# RepetitionDuration 不能给 [TimeSpan]::MaxValue：任务计划会报
# HRESULT 0x80041318（P99999999DT23H59M59S 超出范围）。
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"

$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $logonTrigger, $repeatTrigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Hy-Memory 会话导入：每 5 分钟短时导入，通过 MCP 在服务进程内写入（零 LLM）' `
    | Out-Null

Write-Host '[OK] 计划任务已注册。' -ForegroundColor Green
Write-Host ''
Write-Host '常用命令：'
Write-Host "  schtasks /Run   /TN $TaskName      # 手动触发一次"
Write-Host "  schtasks /End   /TN $TaskName      # 停止正在运行的实例"
Write-Host "  schtasks /Query /TN $TaskName /V   # 查看详情"
Write-Host "  Get-Content `"$baseDir\logs\import_task.log`" -Tail 20 -Encoding UTF8"
