' Chat Manager — Silent Launcher (no console window)
' Double-click this file to start without a command prompt

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = ScriptDir

' Try pythonw.exe (no console), fall back to python.exe
WshShell.Run "pythonw.exe """ & ScriptDir & "\chat-manager-web.py""", 0, False

Set WshShell = Nothing
Set FSO = Nothing
