; Run Jobs installer (Inno Setup 6). Per-user, no admin: installs the PyInstaller build (Python + Tkinter inside) to
; %LOCALAPPDATA%\Programs\RunJobs, adds Start-menu + desktop shortcuts "Run Jobs" and an uninstaller.
; Build: iscc /DAppVersion=<n> /DSrcDir=<pyinstaller dist\RunJobs> /DOutDir=<dir> RunJobs.iss  -> RunJobs-Setup.exe
; The app updates itself by running a newer RunJobs-Setup.exe with /SILENT (sha256-checked first).
#ifndef AppVersion
  #define AppVersion "0"
#endif

[Setup]
AppId={{6E3D9C1A-52F4-4B7E-9A3B-7C1D2E8F4A10}
AppName=Run Jobs
AppVersion={#AppVersion}
AppPublisher=Team Sound, Ocean Hackathon
DefaultDirName={localappdata}\Programs\RunJobs
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=RunJobs-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=yes
UninstallDisplayIcon={app}\RunJobs.exe

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Tasks]
Name: "startmenu"; Description: "Start menu shortcut ""Run Jobs"""
Name: "desktop"; Description: "Desktop shortcut ""Run Jobs"""

[Icons]
Name: "{autoprograms}\Run Jobs"; Filename: "{app}\RunJobs.exe"; Tasks: startmenu
Name: "{autodesktop}\Run Jobs"; Filename: "{app}\RunJobs.exe"; Tasks: desktop

[Run]
Filename: "{app}\RunJobs.exe"; Description: "Open Run Jobs"; Flags: nowait postinstall skipifsilent
; the app's self-update runs this installer with /SILENT /relaunch=1: reopen the app afterwards
Filename: "{app}\RunJobs.exe"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
