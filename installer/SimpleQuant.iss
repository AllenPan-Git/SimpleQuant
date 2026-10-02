; SimpleQuant 安装包（Inno Setup 6）
; 由 build.bat、tools\release.py 调用：先用 PyInstaller 生成 dist\windows\SimpleQuant\，再编译本脚本，输出 dist\windows\SimpleQuant-<版本>-Setup.exe
;   （文件名只用英文：GitHub Releases 会删掉文件名里的中文）
;   ISCC.exe /DAppVersion=0.1.0 installer\SimpleQuant.iss
;
; - 装到 %LOCALAPPDATA%\Programs\SimpleQuant，不需要管理员权限（以后自动更新替换文件时也不会弹确认框）
; - 开始菜单快捷方式；桌面快捷方式可在安装时取消
; - 卸载只删除程序文件；数据在 %LOCALAPPDATA%\SimpleQuant（行情、策略、模拟账户、AI 设置），保留不动
; - 覆盖安装（升级）前先删掉旧的 _internal，避免旧版本残留的库文件
; - 自动更新（补丁）新增的文件不在卸载记录里，卸载时整个删掉 _internal（含 Python 生成的 __pycache__）

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{E1A695AA-65CF-43E6-B7C1-E05D9C531879}
AppName=SimpleQuant
AppVersion={#AppVersion}
AppVerName=SimpleQuant {#AppVersion}
AppPublisher=SimpleQuant
DefaultDirName={autopf}\SimpleQuant
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\gui\static\icon.ico
UninstallDisplayIcon={app}\SimpleQuant.exe
UninstallDisplayName=SimpleQuant
OutputDir=..\dist\windows
OutputBaseFilename=SimpleQuant-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimp.LaunchApp=启动 SimpleQuant
english.LaunchApp=Launch SimpleQuant

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\windows\SimpleQuant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\SimpleQuant"; Filename: "{app}\SimpleQuant.exe"
Name: "{autodesktop}\SimpleQuant"; Filename: "{app}\SimpleQuant.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SimpleQuant.exe"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent
