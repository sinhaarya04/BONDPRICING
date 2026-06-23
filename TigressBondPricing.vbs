' ────────────────────────────────────────────────────────────────────
' Tigress Bond Pricing — silent launcher wrapper.
'
' This is what the desktop shortcut actually launches. It runs
' TigressBondPricing.bat with window state 0 (hidden) so the boss
' never sees a flashing console window — they only see the browser
' tab opening to the login screen.
'
' The .bat itself takes care of starting server.py and opening the
' browser; the .vbs only exists to suppress the cmd window.
' ────────────────────────────────────────────────────────────────────

Set WshShell = CreateObject("WScript.Shell")

' Resolve the folder this .vbs lives in (the install dir) so we
' invoke the .bat with an absolute path regardless of where Windows
' thinks the "current directory" is when the shortcut launches.
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Run "TigressBondPricing.bat" in the install dir.
'   2nd arg = 0   -> window hidden (no cmd flash)
'   3rd arg = False -> don't wait for it to finish before returning
WshShell.Run """" & ScriptDir & "\TigressBondPricing.bat""", 0, False
