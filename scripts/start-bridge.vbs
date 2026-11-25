Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
repoDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
python = fso.BuildPath(repoDir, ".venv\Scripts\python.exe")
WshShell.Run """" & python & """ -m agentaus_bridge", 0, False
