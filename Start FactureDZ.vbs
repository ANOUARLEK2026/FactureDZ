Option Explicit
' Facture DZ launcher. ASCII only on purpose (VBScript mangles Arabic in UTF-8 files).
Dim sh, fso, here, cmd, rc, py
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

Function FirstLine(command)
    On Error Resume Next
    Dim ex
    FirstLine = ""
    Set ex = sh.Exec("cmd /c " & command)
    If Err.Number = 0 Then
        Do While ex.Status = 0
            WScript.Sleep 30
        Loop
        If Not ex.StdOut.AtEndOfStream Then FirstLine = Trim(ex.StdOut.ReadLine())
    End If
End Function

Function ScanFolder(base, exeName)
    On Error Resume Next
    Dim f, sub1, best
    ScanFolder = "" : best = ""
    If Not fso.FolderExists(base) Then Exit Function
    Set f = fso.GetFolder(base)
    For Each sub1 In f.SubFolders
        If fso.FileExists(sub1.Path & "\" & exeName) Then
            If sub1.Name > best Then best = sub1.Name : ScanFolder = sub1.Path & "\" & exeName
        End If
    Next
End Function

Function FindPython()
    Dim p
    p = FirstLine("where pyw.exe 2>nul")
    If p <> "" Then FindPython = """" & p & """ -3" : Exit Function
    p = FirstLine("where pythonw.exe 2>nul")
    If p <> "" Then FindPython = """" & p & """" : Exit Function
    p = ScanFolder(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python", "pythonw.exe")
    If p <> "" Then FindPython = """" & p & """" : Exit Function
    p = ScanFolder(sh.ExpandEnvironmentStrings("%ProgramFiles%"), "pythonw.exe")
    If p <> "" Then FindPython = """" & p & """" : Exit Function
    FindPython = ""
End Function

py = FindPython()
If py = "" Then
    MsgBox "Python 3 was not found." & vbCrLf & "Install it from python.org and tick 'Add python.exe to PATH', then run:" & vbCrLf & "py -m pip install -r requirements.txt", 16, "Facture DZ"
    WScript.Quit 1
End If

cmd = py & " """ & here & "\FactureDZ.pyw"""
rc = sh.Run(cmd, 0, True)
If rc <> 0 And rc <> 2 Then    ' 2 = the app already explained the problem itself
    MsgBox "Facture DZ could not start (code " & rc & ")." & vbCrLf & "Details: " & sh.ExpandEnvironmentStrings("%APPDATA%") & "\FactureDZ\app.log", 48, "Facture DZ"
End If
