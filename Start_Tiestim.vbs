' Double-click this file to start the web server (black CMD window).
' Use this if run_local.bat opens in Notepad instead of running.

Option Explicit
Dim sh, fso, folder, py, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
py = folder & "\.venv\Scripts\python.exe"

If Not fso.FileExists(py) Then
  MsgBox "Missing: " & py & vbCrLf & vbCrLf & "In a terminal, run:" & vbCrLf & _
    "python -m venv .venv" & vbCrLf & _
    ".venv\Scripts\pip install -e ." & vbCrLf & _
    ".venv\Scripts\pip install python-libtiepie", _
    vbCritical, "Tiestim — setup needed"
  WScript.Quit 1
End If

sh.CurrentDirectory = folder
cmd = "cmd.exe /k " & Chr(34) & py & Chr(34) & " -m uvicorn tiestim.api.app:app --host 127.0.0.1 --port 8000"
sh.Run cmd, 1, False
