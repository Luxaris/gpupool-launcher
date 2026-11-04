; Hermes installer (Inno Setup 6). Per-user, no admin: installs the PyInstaller build (Python + Tkinter inside),
; adds Start-menu + desktop shortcuts "Hermes" and an uninstaller.
; Build: iscc /DAppVersion=<n> /DSrcDir=<pyinstaller dist\Hermes> /DOutDir=<dir> Hermes.iss  -> Hermes-Setup.exe
; The app updates itself by running a newer installer with /SILENT /relaunch=1 (sha256-checked first).
; Same AppId as the v1 "Run Jobs" installer: a v1 install is upgraded in place (same folder, same shortcut choices).
#ifndef AppVersion
  #define AppVersion "0"
#endif

[Setup]
AppId={{6E3D9C1A-52F4-4B7E-9A3B-7C1D2E8F4A10}
AppName=Hermes
AppVersion={#AppVersion}
AppPublisher=Team Sound, Ocean Hackathon
DefaultDirName={localappdata}\Programs\Hermes
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=Hermes-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
UninstallDisplayIcon={app}\Hermes.exe

[InstallDelete]
; left over from v1 ("Run Jobs")
Type: files; Name: "{app}\RunJobs.exe"
Type: files; Name: "{autoprograms}\Run Jobs.lnk"
Type: files; Name: "{autodesktop}\Run Jobs.lnk"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Tasks]
Name: "startmenu"; Description: "Start menu shortcut ""Hermes"""
Name: "desktop"; Description: "Desktop shortcut ""Hermes"""

[Icons]
Name: "{autoprograms}\Hermes"; Filename: "{app}\Hermes.exe"; Tasks: startmenu
Name: "{autodesktop}\Hermes"; Filename: "{app}\Hermes.exe"; Tasks: desktop

[Registry]
; Background mode's start-at-login entry is written by the app itself (only when the teammate turns it on);
; the uninstaller removes it. Nothing is written at install time.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Hermes"; ValueType: none; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\Hermes.exe"; Description: "Open Hermes"; Flags: nowait postinstall skipifsilent
; the app's auto-update runs this installer with /VERYSILENT ... /relaunch=1 [/hidden=1]: reopen the app afterwards
; (--updated shows "Hermes updated to vN"; --background goes straight back to the tray if it was hidden there)
Filename: "{app}\Hermes.exe"; Parameters: "{code:RelaunchArgs}"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;

function RelaunchArgs(Param: String): String;
begin
  Result := '--updated';
  if ExpandConstant('{param:hidden|0}') = '1' then
    Result := Result + ' --background';
end;
