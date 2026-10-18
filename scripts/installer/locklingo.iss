; LockLingo per-user installer (Inno Setup 6.7+).
;
; This source is ASCII-only on purpose: every user-visible Chinese string is
; passed in by scripts/build_installer.py through ISCC /D defines, so the file's
; encoding can never garble the installer UI. The Simplified Chinese message
; file is scripts/installer/ChineseSimplified.isl (UTF-8 with BOM).

#ifndef AppName
  #define AppName "LockLingo"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumericVersion
  #define NumericVersion "1.0.0.0"
#endif
#ifndef Publisher
  #define Publisher "piaobo2233"
#endif
#ifndef PublisherURL
  #define PublisherURL "https://github.com/piaobo2233/deadlock-translator"
#endif
#ifndef SourceDir
  #error SourceDir define is required
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef OutputBase
  #define OutputBase "LockLingo-setup"
#endif
#ifndef IconFile
  #error IconFile define is required
#endif
#ifndef LanguageFile
  #define LanguageFile "ChineseSimplified.isl"
#endif
#ifndef RunningMessage
  #define RunningMessage "LockLingo is still running. Exit it from the tray first, then run the installer again."
#endif
#ifndef KeepDataMessage
  #define KeepDataMessage "Keep your personal data (settings, API key, glossary, memory)? Choose No to delete it."
#endif
#ifndef UninstallRunningMessage
  #define UninstallRunningMessage "Exit LockLingo from the tray before uninstalling."
#endif
#ifndef LauncherFile
  #error LauncherFile define is required
#endif

[Setup]
AppId={{8D9E4B3A-2F6C-4E1B-9A7D-2C5F7B1E4A90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#PublisherURL}
AppSupportURL={#PublisherURL}
AppUpdatesURL={#PublisherURL}/releases
VersionInfoVersion={#NumericVersion}
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\LockLingo
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\DeadlockTranslate.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UsePreviousAppDir=yes
CloseApplications=no
RestartApplications=no
AppMutex=DeadlockTranslate_SingleInstance,DeadlockTranslate_InstallGuard
SetupLogging=yes
AllowNoIcons=yes
DisableWelcomePage=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "{#LanguageFile}"

[Messages]
SetupAppRunningError={#RunningMessage}
UninstallAppRunningError={#UninstallRunningMessage}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#LauncherFile}"""; WorkingDir: "{app}"; IconFilename: "{app}\DeadlockTranslate.exe"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#LauncherFile}"""; WorkingDir: "{app}"; IconFilename: "{app}\DeadlockTranslate.exe"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#LauncherFile}"""; Description: "{cm:LaunchProgram,{#AppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CheckForMutexes('DeadlockTranslate_SingleInstance,DeadlockTranslate_InstallGuard') then
    Result := '{#RunningMessage}';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\data');
    if DirExists(DataDir) then
    begin
      { 默认保留：卸载不能悄悄删掉用户的配置、密钥、词库和记忆。
        静默卸载也一律保留，避免自动化误删。 }
      if (not UninstallSilent)
         and (MsgBox('{#KeepDataMessage}', mbConfirmation, MB_YESNO) = IDNO) then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
