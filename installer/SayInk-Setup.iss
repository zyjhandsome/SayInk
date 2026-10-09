; SayInk Installation Script for Inno Setup 6
; Creates a professional Windows installer with custom installation path
;
; Constants are normally passed by build_installer.py:
;   ISCC /DAppVersionStr=2.2.3 /DAppVersionQuad=2.2.3.0 SayInk-Setup.iss
; Lite installer (default): no model inside, the app downloads Fun-ASR-Nano
; on first start. Full installer: /DBundleModel /DOutputSuffix=-full copies
; dist\SayInk\models\ and names the file SayInk-Setup-<version>-full.exe.
; The in-app updater only downloads the lite name (sayink/updater.py).
#ifndef AppVersionStr
#define AppVersionStr "2.2.3"
#endif
#ifndef AppVersionQuad
#define AppVersionQuad "2.2.3.0"
#endif
#ifndef OutputSuffix
#define OutputSuffix ""
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
DirExistsWarning=no
; 确保显示安装路径选择界面
DisableDirPage=no

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
; Paths match build.py PyInstaller output: dist\SayInk\
Source: "..\dist\SayInk\SayInk.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\SayInk\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef BundleModel
; Full installer only: model copied by `build.py --with-model`.
Source: "..\dist\SayInk\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
Name: "{group}\SayInk"; Filename: "{app}\SayInk.exe"
Name: "{group}\访问官网"; Filename: "https://github.com/zyjhandsome/SayInk"
Name: "{group}\卸载 SayInk"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SayInk"; Filename: "{app}\SayInk.exe"; Tasks: desktopicon

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
Type: filesandordirs; Name: "{app}"

[Code]
const
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceInk_is1';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

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

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  // Kill any running SayInk process before installing
  Exec('taskkill', '/F /IM SayInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  // A running VoiceInk holds files in ~\.voiceink open and the rename fails.
  Exec('taskkill', '/F /IM VoiceInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  Sleep(1000);
  MigrateLegacyDataDir();
  UninstallLegacyVoiceInk();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  SayInkDataDir: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    RemovePerUserLeftovers();
    // Ask user if they want to delete user data
    SayInkDataDir := ExpandConstant('{%USERPROFILE}\.sayink');
    if DirExists(SayInkDataDir) then
    begin
      if MsgBox('是否删除用户配置和模型数据？', mbConfirmation, MB_YESNO) = IDYES then
        DelTree(SayInkDataDir, True, True, True);
    end;
  end;
end;