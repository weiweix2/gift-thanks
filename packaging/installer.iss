; ---------------------------------------------------------------------
; 礼物答谢姬（GiftThanks）Inno Setup 安装脚本
;
; 用法（在仓库根目录、PyInstaller 已产出 dist/GiftThanks/ 之后）：
;   iscc packaging/installer.iss
; 产物：packaging/installer/GiftThanks-Setup-1.0.0.exe（OutputDir 相对本脚本目录）
;
; 设计要点（面向电脑小白）：
;   - 中文界面
;   - PrivilegesRequired=lowest：免管理员权限，默认装到
;     %LOCALAPPDATA%\GiftThanks（不会触发 UAC 弹窗）
;   - 桌面快捷方式 + 开始菜单项 + 控制面板卸载程序（Inno 自动生成）
; ---------------------------------------------------------------------

#define MyAppName "礼物答谢姬"
#define MyAppVersion "1.0.0"
#define MyAppExeName "GiftThanks.exe"

[Setup]
; 固定的 AppId（升级安装/卸载识别用），不要随意更换
AppId={{7E9C4A21-5B3D-4F68-9A0E-3C8D1F2B6A54}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=GiftThanks
DefaultDirName={localappdata}\GiftThanks
DefaultGroupName={#MyAppName}
; 免管理员权限：安装到当前用户目录
PrivilegesRequired=lowest
; 输出
OutputDir=installer
OutputBaseFilename=GiftThanks-Setup-1.0.0
; 压缩
Compression=lzma2/ultra64
SolidCompression=yes
; 界面
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
; 只允许 64 位 Windows（PySide6/playwright 均为 x64）
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 版本信息（写进 exe 属性）
VersionInfoVersion=1.0.0.0
VersionInfoDescription={#MyAppName} 安装程序

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; PyInstaller onedir 产物整体拷贝（路径相对于本脚本所在的 packaging/ 目录）
Source: "..\dist\GiftThanks\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autoprograms}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
; 桌面快捷方式（用户勾选了才创建）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 安装结束后勾选「立即运行」
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
