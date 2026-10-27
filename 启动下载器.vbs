' Silent launcher for PersonalDownloader (no CMD window).
' Double-click this file instead of the old .bat to start without a console window.
Dim fso, sh, dir, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then pyw = "pythonw"
sh.CurrentDirectory = dir
sh.Run """" & pyw & """ downloader.py", 0, False
