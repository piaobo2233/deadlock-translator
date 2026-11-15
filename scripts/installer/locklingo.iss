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
#ifndef AppIdValue
  #define AppIdValue "{{8D9E4B3A-2F6C-4E1B-9A7D-2C5F7B1E4A90}"
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
#ifndef PurgeDataMessage
  #define PurgeDataMessage "Keep personal data and local LockLingo backups? Yes keeps them. No permanently deletes them for a full uninstall."
#endif
#ifndef UninstallCleanupError
  #define UninstallCleanupError "LockLingo cleanup could not finish. Close any running LockLingo windows and retry the uninstall."
#endif
#ifndef UninstallRunningMessage
  #define UninstallRunningMessage "Exit LockLingo from the tray before uninstalling."
#endif
#ifndef LauncherFile
  #error LauncherFile define is required
#endif
#ifndef RecoveryFile
  #error RecoveryFile define is required
#endif
#ifndef RecoveryLabel
  #define RecoveryLabel "Restore previous version"
#endif
#ifndef RecoveryError
  #define RecoveryError "Program backup or recovery failed. Use the Start menu recovery entry. See backups\rollback\recovery.log."
#endif
#ifndef RecoveryFailureTitle
  #define RecoveryFailureTitle "Update was not completed"
#endif
#ifndef RecoveryRestored
  #define RecoveryRestored "The new program failed verification. The previous program has been restored; personal data was preserved."
#endif
#ifndef DirectoryMessage
  #define DirectoryMessage "Use an English installation path. Changing folders does not move personal data. Back up the old data folder and move the complete folder manually after installation."
#endif

[Setup]
AppId={#AppIdValue}
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
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#LauncherFile}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UsePreviousAppDir=yes
CloseApplications=no
RestartApplications=no
AppMutex=DeadlockTranslate_SingleInstance,DeadlockTranslate_InstallGuard,Local\LockLingo_RecoveryWindow
SetupLogging=no
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
Source: "{#RecoveryFile}"; DestDir: "{app}\recovery"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#RecoveryLabel}"; Filename: "{app}\recovery\LockLingoRecovery.exe"; Parameters: "--root ""{app}"""; WorkingDir: "{app}"
Name: "{group}\{#AppName}"; Filename: "{app}\{#LauncherFile}"; WorkingDir: "{app}"; IconFilename: "{app}\{#LauncherFile}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#LauncherFile}"; WorkingDir: "{app}"; IconFilename: "{app}\{#LauncherFile}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#LauncherFile}"; Description: "{cm:LaunchProgram,{#AppName}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent; Check: RecoverySucceeded

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
var
  DirectoryNote: TNewStaticText;
  RecoveryPrepared, RecoveryCommitted, RecoveryFailed, RecoveryHadProgram: Boolean;
  RecoveryFailureMessage: String;
  UpdateGuard, InstallerGuard: THandle;

function RecoveryCreateMutex(Attributes: LongWord; InitialOwner: Boolean; Name: String): THandle;
  external 'CreateMutexW@kernel32.dll stdcall';
function RecoveryCloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function RecoveryLastError(): LongWord;
  external 'GetLastError@kernel32.dll stdcall';

procedure ReleaseUpdateGuard();
begin
  if UpdateGuard <> 0 then begin
    RecoveryCloseHandle(UpdateGuard);
    UpdateGuard := 0;
  end;
end;

function RecoverySucceeded(): Boolean;
begin
  Result := RecoveryCommitted;
end;

function RunRecovery(Parameters: String): Boolean;
var Code: Integer;
begin
  Result := Exec(ExpandConstant('{tmp}\LockLingoRecovery.exe'),
    '--root ' + AddQuotes(ExpandConstant('{app}')) + ' ' + Parameters,
    ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure InitializeWizard();
begin
  DirectoryNote := TNewStaticText.Create(WizardForm);
  DirectoryNote.Parent := WizardForm.SelectDirPage;
  DirectoryNote.Left := WizardForm.DirEdit.Left;
  DirectoryNote.Top := WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(12);
  DirectoryNote.Width := WizardForm.SelectDirBrowseLabel.Width;
  DirectoryNote.AutoSize := False;
  DirectoryNote.Height := ScaleY(100);
  DirectoryNote.WordWrap := True;
  DirectoryNote.Caption := '{#DirectoryMessage}';
  if DirExists(WizardDirValue() + '\data') then
    DirectoryNote.Caption := DirectoryNote.Caption + #13#10 + WizardDirValue() + '\data';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var Parameters: String; UpdateActive: Boolean;
begin
  Result := '';
  if RecoveryPrepared then Exit;
  if CheckForMutexes('DeadlockTranslate_SingleInstance,DeadlockTranslate_InstallGuard,Local\LockLingo_RecoveryWindow') then begin
    Result := '{#RunningMessage}'; Exit;
  end;
  InstallerGuard := RecoveryCreateMutex(0, False, 'Local\LockLingo_Installer');
  if (InstallerGuard = 0) or (RecoveryLastError() = 183) then begin
    if InstallerGuard <> 0 then RecoveryCloseHandle(InstallerGuard);
    InstallerGuard := 0; Result := '{#RecoveryError}'; Exit;
  end;
  UpdateGuard := RecoveryCreateMutex(0, False, 'Local\LockLingo_UpdateInProgress');
  UpdateActive := RecoveryLastError() = 183;
  if UpdateGuard = 0 then Result := '{#RecoveryError}'
  else begin
    if CheckForMutexes('DeadlockTranslate_SingleInstance,DeadlockTranslate_InstallGuard,Local\LockLingo_RecoveryWindow') then
      Result := '{#RunningMessage}'
    else begin
      ExtractTemporaryFile('LockLingoRecovery.exe');
      Parameters := '--prepare {#AppVersion} --setup ' + AddQuotes(ExpandConstant('{srcexe}'));
      if UpdateActive then Parameters := Parameters + ' --update-active';
      RecoveryHadProgram := FileExists(ExpandConstant('{app}\VERSION'));
      RecoveryPrepared := RunRecovery(Parameters);
      if not RecoveryPrepared then Result := '{#RecoveryError}';
    end;
  end;
  if Result <> '' then begin
    ReleaseUpdateGuard(); RecoveryCloseHandle(InstallerGuard); InstallerGuard := 0;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    RecoveryCommitted := RunRecovery('--commit');
    if not RecoveryCommitted then begin
      RecoveryFailed := True;
      RecoveryFailureMessage := '{#RecoveryError}';
      if RunRecovery('--abort') then begin
        RecoveryPrepared := False;
        if RecoveryHadProgram then RecoveryFailureMessage := '{#RecoveryRestored}';
      end;
      Log(RecoveryFailureMessage);
    end;
    if RecoveryCommitted or not RecoveryPrepared then ReleaseUpdateGuard();
  end;
end;

function GetCustomSetupExitCode(): Integer;
begin
  Result := 0;
  if RecoveryFailed then Result := 12;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and RecoveryFailed then begin
    WizardForm.FinishedHeadingLabel.Caption := '{#RecoveryFailureTitle}';
    WizardForm.FinishedLabel.Caption := RecoveryFailureMessage;
  end;
end;

procedure DeinitializeSetup();
begin
  if RecoveryPrepared and not RecoveryCommitted then
    if not RunRecovery('--abort') then Log('{#RecoveryError}');
  ReleaseUpdateGuard();
  if InstallerGuard <> 0 then RecoveryCloseHandle(InstallerGuard);
end;

function PurgeUserData(): Boolean;
var Index: Integer;
begin
  Result := False;
  if UninstallSilent then begin
    for Index := 1 to ParamCount do
      if CompareText(ParamStr(Index), '/PURGEDATA') = 0 then Result := True;
  end else
    Result := MsgBox('{#PurgeDataMessage}', mbConfirmation, MB_YESNO) = IDNO;
end;

procedure CleanupFailed();
begin
  Log('{#UninstallCleanupError}');
  SuppressibleMsgBox('{#UninstallCleanupError}', mbError, MB_OK, IDOK);
  Abort;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Parameters: String; Code: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  Parameters := '--uninstall-cleanup --root ' + AddQuotes(ExpandConstant('{app}'));
  if PurgeUserData() then Parameters := Parameters + ' --purge-data';
  InstallerGuard := RecoveryCreateMutex(0, False, 'Local\LockLingo_Installer');
  if (InstallerGuard = 0) or (RecoveryLastError() = 183) then CleanupFailed();
  UpdateGuard := RecoveryCreateMutex(0, False, 'Local\LockLingo_UpdateInProgress');
  if (UpdateGuard = 0) or (RecoveryLastError() = 183) then CleanupFailed();
  if not Exec(ExpandConstant('{app}\recovery\LockLingoRecovery.exe'), Parameters,
    ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code) then CleanupFailed();
  if Code <> 0 then CleanupFailed();
end;

procedure DeinitializeUninstall();
begin
  ReleaseUpdateGuard();
  if InstallerGuard <> 0 then begin
    RecoveryCloseHandle(InstallerGuard);
    InstallerGuard := 0;
  end;
end;
