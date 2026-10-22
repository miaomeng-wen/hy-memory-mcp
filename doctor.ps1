#!/usr/bin/env pwsh
# 校验 Hy-Memory 服务是否正常：端口、HTTP、配置、日志
param([int]$Port = 19527, [int]$ConfigPort = 19528)

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = $true

Write-Host '=== Hy-Memory 服务诊断 ===' -ForegroundColor Cyan

# 1. 端口
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Host "[OK] MCP 端口 $Port 监听中 (PID $($listen.OwningProcess))" -ForegroundColor Green
} else {
    Write-Host "[X ] MCP 端口 $Port 未监听" -ForegroundColor Red
    $ok = $false
}

# 2. HTTP 可达（MCP 对无 Accept 头的普通 GET 返回 406，属正常）
if ($listen) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/mcp" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        Write-Host "[OK] /mcp 返回 $($resp.StatusCode)" -ForegroundColor Green
    } catch {
        $errResp = $_.Exception.Response
        $code = 0
        if ($null -ne $errResp) { $code = [int]$errResp.StatusCode }
        if ($code -eq 406 -or $code -eq 400) {
            Write-Host "[OK] /mcp 响应 $code，协议要求带 Accept 头，属正常" -ForegroundColor Green
        } else {
            Write-Host "[X ] /mcp 异常: $($_.Exception.Message)" -ForegroundColor Red
            $ok = $false
        }
    }
}

# 3. 配置界面端口（可选，不影响记忆服务）
$cfg = Get-NetTCPConnection -LocalPort $ConfigPort -State Listen -ErrorAction SilentlyContinue
if ($cfg) {
    Write-Host "[  ] 配置界面 $ConfigPort 运行中"
} else {
    Write-Host "[  ] 配置界面 $ConfigPort 未运行，不影响记忆服务"
}

# 4. .env
$envFile = Join-Path $baseDir '.env'
if (Test-Path $envFile) {
    $content = Get-Content $envFile -Raw -Encoding UTF8
    $mode = '?'
    $llm = ''
    $emb = '?'
    if ($content -match 'MEMORY_MODE="([^"]+)"') { $mode = $Matches[1] }
    if ($content -match 'MEMORY_LLM_MODEL="([^"]*)"') { $llm = $Matches[1] }
    if ($content -match 'MEMORY_EMBEDDER_MODEL="([^"]+)"') { $emb = $Matches[1] }
    $llmShow = if ($llm) { $llm } else { '未配置' }
    Write-Host "[OK] .env 存在  mode=$mode  embedding=$emb  llm=$llmShow" -ForegroundColor Green
} else {
    Write-Host '[X ] .env 缺失，服务无法工作' -ForegroundColor Red
    $ok = $false
}

# 5. 日志近期错误
$logFile = Join-Path $baseDir 'logs\server.log'
if (Test-Path $logFile) {
    $hits = Get-Content $logFile -Tail 200 -Encoding UTF8 | Select-String 'ERROR|WARNING' | Select-Object -Last 5
    if ($hits) {
        Write-Host '[  ] 最近 5 条警告/错误：' -ForegroundColor Yellow
        foreach ($h in $hits) { Write-Host "     $($h.Line)" -ForegroundColor DarkYellow }
    } else {
        Write-Host '[OK] 日志中无近期错误' -ForegroundColor Green
    }
} else {
    Write-Host '[  ] 尚无日志文件'
}

Write-Host ''
if ($ok) {
    Write-Host '结论：服务正常' -ForegroundColor Green
} else {
    Write-Host '结论：存在问题，见上方 [X ] 项' -ForegroundColor Red
}
