' Buddy2api one-click launcher - run the batch silently (window style 0)
' Usage: wscript.exe start-oneclick.vbs "<bat full path>" [args...]
Option Explicit

Dim sh, fso, i, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count < 1 Then WScript.Quit 1

cmd = """" & WScript.Arguments(0) & """"
For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " " & WScript.Arguments(i)
Next

sh.CurrentDirectory = fso.GetParentFolderName(WScript.Arguments(0))
sh.Run "cmd /c " & cmd, 0, False
