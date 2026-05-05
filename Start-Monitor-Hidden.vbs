' Starts monitor using the same Python search order as Start-Monitor.bat
Option Explicit

Dim fso, sh, folder, exe, candidates, i, p, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = folder

exe = ""
candidates = Array( _
  sh.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python314\pythonw.exe", _
  sh.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python313\pythonw.exe", _
  sh.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python312\pythonw.exe", _
  sh.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python311\pythonw.exe" _
)
For i = 0 To UBound(candidates)
  p = candidates(i)
  If fso.FileExists(p) Then
    exe = p
    Exit For
  End If
Next

If exe = "" Then
  If fso.FileExists(sh.ExpandEnvironmentStrings("%SystemRoot%") & "\pyw.exe") Then
    cmd = Chr(34) & sh.ExpandEnvironmentStrings("%SystemRoot%") & "\pyw.exe" & Chr(34) & " -3 " & Chr(34) & folder & "\monitor.py" & Chr(34) & " --dock --history --tray"
    sh.Run cmd, 0, False
    WScript.Quit 0
  End If
  If fso.FileExists(sh.ExpandEnvironmentStrings("%SystemRoot%") & "\py.exe") Then
    cmd = Chr(34) & sh.ExpandEnvironmentStrings("%SystemRoot%") & "\py.exe" & Chr(34) & " -3 " & Chr(34) & folder & "\monitor.py" & Chr(34) & " --dock --history --tray"
    sh.Run cmd, 0, False
    WScript.Quit 0
  End If
  exe = "pythonw.exe"
End If

cmd = Chr(34) & exe & Chr(34) & " " & Chr(34) & folder & "\monitor.py" & Chr(34) & " --dock --history --tray"
sh.Run cmd, 0, False
