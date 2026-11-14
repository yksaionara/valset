; valset setup (Inno Setup 6). Built by build.ps1, which passes /DAppVersion=<VERSION from valset.py>.
; Installs for the current user only (no admin prompt) into %LOCALAPPDATA%\Programs\valset,
; a folder valset can write to, so saves sit next to the exe and the built-in updater can swap it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6552B0F9-2943-460D-A405-DC83D0B05303}
AppName=valset
AppVersion={#AppVersion}
AppVerName=valset {#AppVersion}
AppPublisher=yksaionara
AppPublisherURL=https://github.com/yksaionara/valset
AppSupportURL=https://github.com/yksaionara/valset/issues
AppUpdatesURL=https://github.com/yksaionara/valset/releases
DefaultDirName={localappdata}\Programs\valset
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=valset-setup
SetupIconFile=..\assets\valset.ico
UninstallDisplayIcon={app}\valset.exe
UninstallDisplayName=valset
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
VersionInfoVersion={#Copy(AppVersion, 1, Pos("-", AppVersion + "-") - 1)}
VersionInfoProductName=valset
VersionInfoDescription=valset setup

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\valset.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\valset"; Filename: "{app}\valset.exe"; Comment: "VALORANT live match tracker and settings saver"
Name: "{autodesktop}\valset"; Filename: "{app}\valset.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\valset.exe"; Description: "Open valset now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; left behind by the built-in updater and the options screen
Type: files; Name: "{app}\valset.old.exe"
Type: files; Name: "{app}\valset.new.exe"
Type: files; Name: "{app}\valset-window.json"

[Code]
// Saves live in {app}\profiles. Keep them unless the user says otherwise.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}\profiles')) then
    if UninstallSilent or
       (MsgBox('Also delete your saved VALORANT settings (the profiles folder)?' + #13#10#13#10 +
               'Choose No to keep them for next time.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES) then
      exit
    else
      DelTree(ExpandConstant('{app}\profiles'), True, True, True);
  if CurUninstallStep = usPostUninstall then
    RemoveDir(ExpandConstant('{app}'));
end;
