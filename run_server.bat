@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Hy-Memory MCP server...
echo Endpoint: http://127.0.0.1:19527/mcp
echo.
".venv\Scripts\python.exe" memory_server.py
pause
