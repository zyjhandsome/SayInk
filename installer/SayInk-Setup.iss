; SayInk Installation Script for Inno Setup 6
; Creates a professional Windows installer with custom installation path
;
; Version constants are normally passed by build_installer.py:
;   ISCC /DAppVersionStr=2.2.1 /DAppVersionQuad=2.2.1.0 SayInk-Setup.iss
#ifndef AppVersionStr
#define AppVersionStr "2.2.1"
#endif
#ifndef AppVersionQuad
#define AppVersionQuad "2.2.1.0"
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
OutputBaseFilename=SayInk-Setup-{#AppVersionStr}
SetupIconFile=..\sayink\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
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

[Files]
; Paths match build.py PyInstaller output: dist\SayInk\
Source: "..\dist\SayInk\SayInk.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\SayInk\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; Models copied by build.py when present (optional at compile time)
Source: "..\dist\SayInk\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\SayInk"; Filename: "{app}\SayInk.exe"
Name: "{group}\访问官网"; Filename: "https://github.com/zyjhandsome/SayInk"
Name: "{group}\卸载 SayInk"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SayInk"; Filename: "{app}\SayInk.exe"; Tasks: desktopicon

[Registry]
; Auto-start on Windows boot (optional)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SayInk"; ValueData: """{app}\SayInk.exe"""; Tasks: autostart; Flags: uninsdeletevalue
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

// VoiceInk ≤ 2.1.0 kept its data in ~\.voiceink. Rename it before the old
// uninstaller runs so its「是否删除用户配置」prompt never sees the folder;
// the app performs the same move on first start for portable copies.
procedure MigrateLegacyDataDir();
var
  OldDir, NewDir: string;
begin
  OldDir := ExpandConstant('{%USERPROFILE}\.voiceink');
  NewDir := ExpandConstant('{%USERPROFILE}\.sayink');
  if DirExists(OldDir) and (not DirExists(NewDir)) then
  begin
    if RenameFile(OldDir, NewDir) then
      Log('Moved ' + OldDir + ' to ' + NewDir)
    else
      Log('Could not move ' + OldDir + '; the app will retry on first start');
  end;
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
  Log('Removing legacy VoiceInk via ' + UninstallString);
  Exec('taskkill', '/F /IM VoiceInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
  Exec(UninstallString, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  // Kill any running SayInk process before installing
  Exec('taskkill', '/F /IM SayInk.exe', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
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
    // Ask user if they want to delete user data
    SayInkDataDir := ExpandConstant('{%USERPROFILE}\.sayink');
    if DirExists(SayInkDataDir) then
    begin
      if MsgBox('是否删除用户配置和模型数据？', mbConfirmation, MB_YESNO) = IDYES then
        DelTree(SayInkDataDir, True, True, True);
    end;
  end;
end;