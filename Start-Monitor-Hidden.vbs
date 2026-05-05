' Double-click to start the monitor (no console window). Python must be on PATH.
Option Explicit
Dim sh, fso, folder
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = folder
sh.Run "pythonw.exe monitor.py --dock --history --tray", 0, False
