<#
.SYNOPSIS
    卸载 Hy-Memory MCP 的开机自启计划任务。

.DESCRIPTION
    仅移除计划任务，不动服务文件、.env 和 data 目录。
    如果服务此刻正在运行，会询问是否一并停止。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall_autostart.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'HyMemoryMCP',
    [switch]$StopService
)

$ErrorActionPreference = 'Stop'
$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host '[X] 需要管理员权限。' -ForegroundColor Red
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "[i] 未找到计划任务 $TaskName，无需卸载。" -ForegroundColor Yellow
} else {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] 已移除计划任务 $TaskName" -ForegroundColor Green
}

$running = Get-NetTCPConnection -LocalPort 19527 -State Listen -ErrorAction SilentlyContinue
if ($running) {
    if ($StopService) {
        $ans = 'y'
    } else {
        $ans = Read-Host '服务仍在运行，是否停止？(y/N)'
    }
    if ($ans -eq 'y') {
        & (Join-Path $baseDir 'stop_server.bat')
    }
}

Write-Host ''
Write-Host '以下内容未被删除：'
Write-Host "  .env      $baseDir\.env"
Write-Host "  data      $baseDir\data"
Write-Host "  logs      $baseDir\logs"
Write-Host '如需彻底清理，手动删除整个目录即可。'
