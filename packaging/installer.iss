; Inno Setup script — compile with ISCC.exe after running PyInstaller.
; Installs per-machine; business data lives in %APPDATA%\NextGenERP and is NEVER removed by uninstall.
[Setup]
AppName=NextGen ERP
AppVersion=1.0
AppPublisher=New Age Systems
DefaultDirName={autopf}\NextGen ERP
DefaultGroupName=NextGen ERP
OutputBaseFilename=NextGenERP-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
; SignTool=...   <- sign the installer and exe with a code-signing certificate to avoid SmartScreen warnings
[Files]
Source: "..\dist\NextGenERP\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
[Icons]
Name: "{group}\NextGen ERP"; Filename: "{app}\NextGenERP.exe"
Name: "{autodesktop}\NextGen ERP"; Filename: "{app}\NextGenERP.exe"
[Run]
Filename: "{app}\NextGenERP.exe"; Description: "Start NextGen ERP"; Flags: nowait postinstall skipifsilent
