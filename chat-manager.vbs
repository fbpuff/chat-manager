' Chat Manager — Silent Launcher (no console window)
' Double-click this file to start without a command prompt

On Error Resume Next

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = ScriptDir
ScriptPath = """" & ScriptDir & "\chat-manager-web.py"""

' Try pythonw.exe first (no console), then python.exe (may flash a window)
Dim result
result = WshShell.Run("pythonw.exe " & ScriptPath, 0, False)
If result <> 0 Then
    result = WshShell.Run("python.exe " & ScriptPath, 0, False)
End If

If result <> 0 Then
    MsgBox "无法启动 Chat Manager。" & vbCrLf & vbCrLf & _
           "请确保 Python 已安装并添加到 PATH 环境变量。" & vbCrLf & _
           "或从 https://github.com/fbpuff/chat-manager/releases 下载 EXE 版本。", _
           vbCritical, "Chat Manager"
End If

Set WshShell = Nothing
Set FSO = Nothing
