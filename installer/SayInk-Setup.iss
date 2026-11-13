; SayInk Installation Script for Inno Setup 6
; Creates a professional Windows installer with custom installation path
;
; Constants are normally passed by build_installer.py:
;   ISCC /DAppVersionStr=2.2.35 /DAppVersionQuad=2.2.35.0 SayInk-Setup.iss
; Both installers copy dist\SayInk\models\. Lite has CAM++, Silero VAD and
; the experimental speaker-activity model; the app downloads Fun-ASR-Nano on
; first start. Full installer:
; /DOutputSuffix=-full, and models\ also contains Fun-ASR-Nano.
; The in-app updater only downloads the lite name (sayink/updater.py).
#ifndef AppVersionStr
#define AppVersionStr "2.2.35"
#endif
#ifndef AppVersionQuad
#define AppVersionQuad "2.2.35.0"
#endif
#ifndef OutputSuffix
#define OutputSuffix ""
#endif
#ifndef SayInkStagingDir
#define SayInkStagingDir "..\dist\SayInk"
#endif

[Setup]
; Fixed id: upgrades replace earlier SayInk installs instead of stacking.
; (VoiceInk ≤ 2.1.0 used the implicit id "VoiceInk"; InitializeSetup below
; removes that install first so the two names never coexist.)
AppId={{SayInk}}
AppName=SayInk
AppVersion={#AppVersionStr}
AppPublisher=SayInk
AppPublisherURL=https://github.com/zyjhandsome/SayInk
AppSupportURL=https://github.com/zyjhandsome/SayInk/issues
DefaultDirName={commonpf}\SayInk
DefaultGroupName=SayInk
AllowNoIcons=yes
OutputDir=..\dist
OutputBaseFilename=SayInk-Setup-{#AppVersionStr}{#OutputSuffix}
SetupIconFile=..\sayink\icon.ico
Compression=lzma2/ultra64
; 模型本身已经压缩过，整包固体压缩几乎不再变小，但双击后要先解开
; 固体流，向导才会出现，资源管理器会一直卡住。关掉后窗口马上出来。
SolidCompression=no
WizardStyle=modern
; 只带简体中文，避免向导页中英混排，也不弹出语言选择。
ShowLanguageDialog=no
PrivilegesRequired=admin
UninstallDisplayIcon={app}\SayInk.exe
UninstallDisplayName=SayInk
DirExistsWarning=auto
; 确保显示安装路径选择界面
DisableDirPage=no
#ifdef UseSignTool
; Optional Authenticode signing for Setup + Uninstaller when build_installer.py
; runs with SAYINK_SIGNTOOL_CMD and SAYINK_INNO_SIGNTOOL=1.
SignTool=sayinksign
SignedUninstaller=yes
#endif

; Version info
VersionInfoVersion={#AppVersionQuad}
VersionInfoCompany=SayInk
VersionInfoDescription=SayInk 安装程序
VersionInfoCopyright=SayInk
VersionInfoProductName=SayInk
VersionInfoProductVersion={#AppVersionQuad}

[Languages]
; 语言包放在脚本旁边。当前安装的 Inno Setup 的 Languages 目录里没有简体中文。
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "开机自动启动"; GroupDescription: "启动选项"; Flags: unchecked

[InstallDelete]
; Libraries dropped by a newer build must not linger next to the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Paths match build_installer.py SayInkStagingDir (default ..\dist\SayInk)
Source: "{#SayInkStagingDir}\SayInk.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SayInkStagingDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; Lite and full both ship models\. Upgrade does not delete {app}\models, so a
; previously installed Fun-ASR-Nano stays when a lite package is applied.
Source: "{#SayInkStagingDir}\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SayInk"; Filename: "{app}\SayInk.exe"; AppUserModelID: "SayInk.SayInkApp"
Name: "{group}\访问官网"; Filename: "https://github.com/zyjhandsome/SayInk"
Name: "{group}\卸载 SayInk"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SayInk"; Filename: "{app}\SayInk.exe"; AppUserModelID: "SayInk.SayInkApp"; Tasks: desktopicon

[Registry]
; The autostart task is written in [Code] for the signed-in user: HKCU here
; would be the hive of whichever admin approved the UAC prompt.
; App paths for Windows to find the executable
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\SayInk.exe"; ValueType: string; ValueName: ""; ValueData: "{app}\SayInk.exe"; Flags: uninsdeletekey

[Run]
Filename: "{app}\SayInk.exe"; Description: "立即运行 SayInk"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM SayInk.exe"; Flags: runhidden waituntilterminated

[UninstallDelete]
; Never delete {app} recursively with wildcards: if a user installed into a
; shared parent directory, filesandordirs on {app} would destroy unrelated files.
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
Type: dirifempty; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Code]
const
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceInk_is1';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  ProfileListKey = 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList';
  SingleInstanceMutexName = 'Local\SayInk_Single_Instance_Mutex';
  SYNCHRONIZE_ACCESS = $00100000;
  CRED_TYPE_GENERIC = 1;

function OpenProcess(dwDesiredAccess: DWORD; bInheritHandle: BOOL; dwProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: DWORD): DWORD;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';
function CredDeleteW(TargetName: string; CredType: DWORD; Flags: DWORD): BOOL;
  external 'CredDeleteW@advapi32.dll stdcall';

// Setup runs elevated, possibly as another account (a standard user typing an
// admin password). Per-user data, the Run key and Start Menu entries belong
// to the user who started Setup, so those commands run as that user and let
// %USERPROFILE% / %APPDATA% / HKCU resolve to their profile.
function RunAsUser(const Filename, Params: string): Integer;
var
  ResultCode: Integer;
begin
  if ExecAsOriginalUser(Filename, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode
  else
    Result := -1;
end;

// VoiceInk ≤ 2.1.0 kept its data in ~\.voiceink. Rename it before the old
// uninstaller runs so its「是否删除用户配置」prompt never sees the folder;
// if a file is locked the app retries the move on its next start.
procedure MigrateLegacyDataDir();
begin
  RunAsUser(ExpandConstant('{cmd}'),
    '/c if exist "%USERPROFILE%\.voiceink\" if not exist "%USERPROFILE%\.sayink" ' +
    'move "%USERPROFILE%\.voiceink" "%USERPROFILE%\.sayink"');
end;

// Unknown (the check could not run) counts as present: skipping the old
// uninstaller is harmless, running it next to the data is not.
function UserHasLegacyDataDir(): Boolean;
begin
  Result := RunAsUser(ExpandConstant('{cmd}'),
    '/c if exist "%USERPROFILE%\.voiceink\" (exit 1) else (exit 0)') <> 0;
end;

// Remove a VoiceInk install (same program, old name) so both do not sit in
// Program Files, both auto-start, and both fight over the hotkey.
procedure UninstallLegacyVoiceInk();
var
  UninstallString: string;
  ErrorCode: Integer;
begin
  if not RegQueryStringValue(HKLM, LegacyUninstallKey, 'UninstallString', UninstallString) then
    if not RegQueryStringValue(HKCU, LegacyUninstallKey, 'UninstallString', UninstallString) then
      Exit;
  UninstallString := RemoveQuotes(UninstallString);
  if not FileExists(UninstallString) then
    Exit;
  // Its uninstaller asks with a plain MsgBox (not suppressible) whether to
  // delete ~\.voiceink; never let it run while the data still lives there.
  if UserHasLegacyDataDir() then
  begin
    Log('Legacy data folder still present; skipping the VoiceInk uninstaller');
    Exit;
  end;
  Log('Removing legacy VoiceInk via ' + UninstallString);
  if Exec(UninstallString, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode)
    and (ErrorCode = 0) then
    // The old app made this per-user shortcut itself; its uninstaller does
    // not know about it and it now points at a deleted EXE.
    RunAsUser(ExpandConstant('{cmd}'),
      '/c del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\VoiceInk.lnk"');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('autostart') then
    RunAsUser(ExpandConstant('{sys}\reg.exe'),
      'add "HKCU\' + RunKey + '" /v SayInk /t REG_SZ /d "\"' +
      ExpandConstant('{app}') + '\SayInk.exe\"" /f');
end;

// The app writes its own Run value (settings → 开机自启) and a Start Menu
// shortcut (taskbar name/icon) for each user who starts it; Setup never
// recorded either. Clean them for every signed-in user, but only a Run value
// that launches this install, so a source checkout's entry survives.
procedure RemovePerUserLeftovers();
var
  Users: TArrayOfString;
  I: Integer;
  AppExe, Value, Programs: string;
begin
  AppExe := Lowercase(ExpandConstant('{app}\SayInk.exe'));
  if not RegGetSubkeyNames(HKU, '', Users) then
    Exit;
  for I := 0 to GetArrayLength(Users) - 1 do
  begin
    if RegQueryStringValue(HKU, Users[I] + '\' + RunKey, 'SayInk', Value)
      and (Pos(AppExe, Lowercase(Value)) > 0) then
    begin
      RegDeleteValue(HKU, Users[I] + '\' + RunKey, 'SayInk');
      Log('Removed Run\SayInk for ' + Users[I]);
    end;
    if RegQueryStringValue(HKU,
      Users[I] + '\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders',
      'Programs', Programs) then
      DeleteFile(AddBackslash(Programs) + 'SayInk.lnk');
  end;
end;

function DirectoryHasEntries(const DirPath: string): Boolean;
var
  FindRec: TFindRec;
begin
  Result := False;
  if not DirExists(DirPath) then
    Exit;
  if FindFirst(AddBackslash(DirPath) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          Result := True;
          Break;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  SelectedDir, SafeDir, LeafName: string;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    SelectedDir := RemoveBackslashUnlessRoot(Trim(WizardDirValue));
    LeafName := Lowercase(ExtractFileName(SelectedDir));
    if DirExists(SelectedDir)
      and DirectoryHasEntries(SelectedDir)
      and (not FileExists(AddBackslash(SelectedDir) + 'SayInk.exe'))
      and (LeafName <> 'sayink') then
    begin
      SafeDir := AddBackslash(SelectedDir) + 'SayInk';
      WizardForm.DirEdit.Text := SafeDir;
      if not WizardSilent() then
      begin
        if SuppressibleMsgBox(
          '所选目录已包含其他文件，为防止污染现有目录，已自动追加子目录：' + #13#10 +
          SafeDir + #13#10#13#10 + '是否使用该目录继续安装？',
          mbConfirmation,
          MB_YESNO,
          IDYES
        ) <> IDYES then
          Result := False;
      end;
    end;
  end;
end;

procedure WaitForGracefulAppExit();
var
  WaitPidStr: string;
  WaitPid, I, ErrorCode: Integer;
  ProcHandle: THandle;
begin
  WaitPidStr := Trim(ExpandConstant('{param:WAITPID|}'));
  if WaitPidStr = '' then
    WaitPidStr := Trim(GetEnv('SAYINK_UPDATE_WAIT_PID'));
  WaitPid := StrToIntDef(WaitPidStr, 0);
  if WaitPid > 0 then
  begin
    ProcHandle := OpenProcess(SYNCHRONIZE_ACCESS, False, WaitPid);
    if ProcHandle <> 0 then
    begin
      Log('Waiting for updating SayInk PID ' + IntToStr(WaitPid) + ' to exit cleanly');
      WaitForSingleObject(ProcHandle, 12000);
      CloseHandle(ProcHandle);
    end;
  end;

  if CheckForMutexes(SingleInstanceMutexName) then
  begin
    // Request polite exit first before forceful termination so SQLite/Config flush cleanly.
    Exec('taskkill', '/IM SayInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
    for I := 1 to 25 do
    begin
      if not CheckForMutexes(SingleInstanceMutexName) then
        Break;
      Sleep(200);
    end;
  end;

  // Final safety fallback if any SayInk.exe process is still hung.
  Exec('taskkill', '/F /IM SayInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  WaitForGracefulAppExit();
  // A running VoiceInk holds files in ~\.voiceink open and the rename fails.
  Exec('taskkill', '/F /IM VoiceInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  Sleep(500);
  MigrateLegacyDataDir();
  UninstallLegacyVoiceInk();
  Result := True;
end;

function IsRealUserSid(const Sid: string): Boolean;
begin
  Result := (Pos('S-1-5-21-', Sid) = 1) and (Pos('_Classes', Sid) = 0);
end;

procedure AppendUniqueDir(var List: TArrayOfString; const Candidate: string);
var
  I, Len: Integer;
  Norm: string;
begin
  if (Candidate = '') or (not DirExists(Candidate)) then
    Exit;
  Norm := RemoveBackslashUnlessRoot(Candidate);
  Len := GetArrayLength(List);
  for I := 0 to Len - 1 do
  begin
    if CompareText(List[I], Norm) = 0 then
      Exit;
  end;
  SetArrayLength(List, Len + 1);
  List[Len] := Norm;
end;

procedure CollectUserDataDirs(var DataDirs: TArrayOfString);
var
  Sids: TArrayOfString;
  I: Integer;
  HomeDir, AppDataDir: string;
begin
  SetArrayLength(DataDirs, 0);

  if RegGetSubkeyNames(HKU, '', Sids) then
  begin
    for I := 0 to GetArrayLength(Sids) - 1 do
    begin
      if IsRealUserSid(Sids[I]) then
      begin
        if RegQueryStringValue(HKU, Sids[I] + '\Volatile Environment', 'USERPROFILE', HomeDir) then
        begin
          AppendUniqueDir(DataDirs, AddBackslash(HomeDir) + '.sayink');
          AppendUniqueDir(DataDirs, AddBackslash(HomeDir) + '.voiceink');
        end
        else if RegQueryStringValue(
          HKU,
          Sids[I] + '\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders',
          'AppData',
          AppDataDir
        ) then
        begin
          HomeDir := ExtractFileDir(ExtractFileDir(RemoveBackslashUnlessRoot(AppDataDir)));
          AppendUniqueDir(DataDirs, AddBackslash(HomeDir) + '.sayink');
          AppendUniqueDir(DataDirs, AddBackslash(HomeDir) + '.voiceink');
        end;
      end;
    end;
  end;

  if RegGetSubkeyNames(HKLM, ProfileListKey, Sids) then
  begin
    for I := 0 to GetArrayLength(Sids) - 1 do
    begin
      if IsRealUserSid(Sids[I]) and RegQueryStringValue(
        HKLM, ProfileListKey + '\' + Sids[I], 'ProfileImagePath', HomeDir
      ) then
      begin
        AppendUniqueDir(DataDirs, AddBackslash(HomeDir) + '.sayink');
        AppendUniqueDir(DataDirs, AddBackslash(HomeDir) + '.voiceink');
      end;
    end;
  end;
end;

procedure DeleteStoredApiCredentials();
var
  ErrorCode: Integer;
begin
  CredDeleteW('SayInk/llm.api_key', CRED_TYPE_GENERIC, 0);
  CredDeleteW('VoiceInk/llm.api_key', CRED_TYPE_GENERIC, 0);
  Exec(ExpandConstant('{sys}\cmdkey.exe'), '/delete:SayInk/llm.api_key', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  Exec(ExpandConstant('{sys}\cmdkey.exe'), '/delete:VoiceInk/llm.api_key', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDirs: TArrayOfString;
  I: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    RemovePerUserLeftovers();
    CollectUserDataDirs(DataDirs);
    AppendUniqueDir(DataDirs, ExpandConstant('{%USERPROFILE}\.sayink'));
    AppendUniqueDir(DataDirs, ExpandConstant('{%USERPROFILE}\.voiceink'));
    if GetArrayLength(DataDirs) > 0 then
    begin
      if SuppressibleMsgBox(
        '是否删除用户配置、历史记录和模型数据？',
        mbConfirmation,
        MB_YESNO or MB_DEFBUTTON2,
        IDNO
      ) = IDYES then
      begin
        for I := 0 to GetArrayLength(DataDirs) - 1 do
          DelTree(DataDirs[I], True, True, True);
        DeleteStoredApiCredentials();
      end;
    end;
  end;
end;