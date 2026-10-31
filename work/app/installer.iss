; OnseiCraft 安装包（Inno Setup 6）
;
; 构建顺序：
;   1) python -m PyInstaller --noconfirm --clean ^
;        --distpath work/tmp/pydist --workpath work/tmp/pybuild work/app/OnseiCraft.spec
;   2) "C:\...\Inno Setup 6\ISCC.exe" work/app/installer.iss
;
; 产物：work/tmp/installer/OnseiCraft-<版本>-Setup.exe
;
; 为什么默认装到 {autopf}（lowest 权限下 = %LOCALAPPDATA%\Programs）而不是 Program Files：
; 程序要在自己目录下写 work\runs、work\data（每次生成字幕都写），
; Program Files 不可写，装进去就得靠管理员权限运行，反而更差。
; 用户仍可在安装向导里改成任意目录（D:\OnseiCraft 之类）。

#define MyAppName "OnseiCraft"
#define MyAppVersion "0.1.1"
#define MyAppExeName "OnseiCraft.exe"
#define BuildDir "..\tmp\pydist\OnseiCraft"

[Setup]
AppId={{9E4C1A62-3F7B-4C8E-9D21-6A5B8E0F31C7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=OnseiCraft
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\tmp\installer
OutputBaseFilename=OnseiCraft-{#MyAppVersion}-Setup
SetupIconFile=studio\assets\OnseiCraft.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
ShowLanguageDialog=no
DisableDirPage=no

[Languages]
Name: "zh"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
; 默认勾选：用户要的就是「装完桌面上有个图标，双击就能开」
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"

[Files]
; 程序本体（PyInstaller onedir：exe + _internal）
Source: "{#BuildDir}\*"; DestDir: "{app}"; Excludes: "work"; Flags: ignoreversion recursesubdirs createallsubdirs
; 管线代码与公开知识包（子进程直接跑这些 .py，不打进 exe）
; ⚠️ 必须排除 pipeline.local.json —— 那是**本机**模型路径（开发者的 D:\ 绝对路径），
;    打进安装包会让新用户一启动就指向一个不存在的目录，而且属于泄露本机信息。
Source: "..\scripts\*"; DestDir: "{app}\work\scripts"; Excludes: "__pycache__,*.pyc,*.log,.bak*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\pipeline\*"; DestDir: "{app}\work\pipeline"; Excludes: "pipeline.local.json,__pycache__,*.pyc,*.log"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\presets\*"; DestDir: "{app}\work\presets"; Excludes: "__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清运行时缓存；work\data（ASR 暂存、锁、反馈）、work\runs 与 models\（约 7.5GB 模型）
; **故意不删** —— 卸载重装不该让用户重下 7.5GB，也不该丢他的人工修正。
Type: filesandordirs; Name: "{app}\work\tmp"
Type: filesandordirs; Name: "{app}\work\data\tmp"
