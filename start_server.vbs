' 隐藏窗口启动 Hy-Memory MCP 服务。
' 由任务计划程序调用（wscript.exe start_server.vbs），也可手动双击。
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = baseDir

' 0 = 隐藏窗口，False = 不等待
shell.Run """" & baseDir & "\.venv\Scripts\python.exe"" memory_server.py", 0, False
