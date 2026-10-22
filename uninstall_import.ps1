<#
.SYNOPSIS
    卸载“会话导入”计划任务。

.DESCRIPTION
    只删除计划任务定义，不删除任何已导入的记忆、状态文件或日志。
    停止导入后：data\import_state_v2.json 保留待办，随时可重新安装续跑。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall_import.ps1
#>

[CmdletBinding()]
param([string]$TaskName = 'HyMemoryImport')

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "[i] 任务 $TaskName 不存在，无需卸载。" -ForegroundColor Yellow
    exit 0
}

# 正在运行的实例先停掉，否则注册表项删除后子进程会继续跑
try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Host '[i] 没有正在运行的实例。'
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "[OK] 已卸载计划任务 $TaskName。" -ForegroundColor Green
Write-Host '    已导入的记忆与 data\import_state_v2.json 均未改动。'
