; blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
; Copyright (C) 2026 Pranjal Tripathi
; SPDX-License-Identifier: GPL-3.0-or-later
;
; Inno Setup script for the BLAZE Control installer. Build after PyInstaller:
;   set BLAZE_VERSION=0.3.2
;   iscc packaging\installer.iss
; Output: dist\BLAZE-Control-Setup-<version>.exe

#define AppName "BLAZE Control"
#define AppVersion GetEnv("BLAZE_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
; AppId identifies the app for upgrades/uninstall. Never change it.
AppId={{28705252-2EBE-4E1D-9A47-1AC01157C204}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Pranjal Tripathi
AppPublisherURL=https://github.com/pranjaltripathi684-wq/blazectl
AppSupportURL=https://github.com/pranjaltripathi684-wq/blazectl/issues
AppCopyright=Copyright (C) 2026 Pranjal Tripathi
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=BLAZE-Control-Setup-{#AppVersion}
SetupIconFile=blaze.ico
UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Installs for the current user without admin rights; the user can choose "all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close a running copy before upgrading
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
