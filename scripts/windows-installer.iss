; Per-user unsigned beta installer. Payload is the already qualified portable folder.
#ifndef BundleRoot
  #error BundleRoot is required
#endif
#ifndef Target
  #error Target is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef OutputRoot
  #error OutputRoot is required
#endif
#if Target == "windows-arm64"
  #define AllowedArchitecture "arm64"
#elif Target == "windows-x64"
  #define AllowedArchitecture "x64os"
#else
  #error Unsupported native payload target
#endif

[Setup]
AppId=OpenClank.Beta1.{#Target}
AppName=Open Clank Beta 1 ({#Target})
AppVersion={#AppVersion}
AppVerName=Open Clank {#AppVersion} Beta 1 ({#Target})
AppPublisher=Open Clank
AppPublisherURL=https://github.com/Plaer1/open-clank
DefaultDirName={localappdata}\Programs\Open Clank Beta\{#Target}
DefaultGroupName=Open Clank Beta ({#Target})
PrivilegesRequired=lowest
ArchitecturesAllowed={#AllowedArchitecture}
ArchitecturesInstallIn64BitMode={#AllowedArchitecture}
SetupArchitecture=x64
MinVersion=10.0.22000
UninstallDisplayIcon={app}\payload\openclank.exe
UninstallFilesDir={app}
OutputDir={#OutputRoot}
OutputBaseFilename=Open-Clank-{#AppVersion}-{#Target}-Setup
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
AlwaysRestart=no
SignedUninstaller=no
DisableProgramGroupPage=no
UsePreviousAppDir=no
UsePreviousTasks=no
SetupLogging=yes

[Tasks]
Name: "downloadart"; Description: "Install complete offline emoji artwork (download about 2.2 GB, or use existing release parts)"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#BundleRoot}\*"; DestDir: "{app}\payload"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Open Clank"; Filename: "{app}\payload\openclank.exe"; Parameters: "server start --open-browser"; WorkingDir: "{app}\payload"
Name: "{group}\Stop Open Clank"; Filename: "{app}\payload\openclank.exe"; Parameters: "server stop"; WorkingDir: "{app}\payload"
Name: "{group}\Uninstall Open Clank"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Open Clank Beta ({#Target})"; Filename: "{app}\payload\openclank.exe"; Parameters: "server start --open-browser"; WorkingDir: "{app}\payload"; Tasks: desktopicon

[Code]
const
  ArtworkBaseURL = 'https://github.com/Plaer1/open-clank/releases/download/v1.0.2-beta.1/';
  ArtworkBytes = 2211209216;
var
  ArtworkPage: TInputDirWizardPage;
  DownloadPage: TDownloadWizardPage;
  PartsDirectory: String;

procedure InitializeWizard;
begin
  ArtworkPage := CreateInputDirPage(wpSelectTasks, 'Offline emoji artwork',
    'Download the complete artwork or use existing release files',
    'Leave the folder empty to download the verified release artwork with progress. ' +
    'If you already have all five parts and emoji-assets.parts.json, select their folder. ' +
    'Artwork stays in your personal application data and is preserved on uninstall. ' +
    'This beta installer is unsigned.', False, '');
  ArtworkPage.Add('Existing artwork parts folder (optional):');
  ArtworkPage.Values[0] := ExpandConstant('{param:ARTWORKPARTS|}');
  DownloadPage := CreateDownloadPage('Downloading offline emoji artwork',
    'Downloading about 2.2 GB from the published Open Clank Beta release; verifying every file.', nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  WizardForm.FinishedLabel.Caption :=
    'Open Clank and its private Python runtime are installed for your Windows account. ' +
    'Launch Open Clank from the Start menu and create your account in the browser. ' +
    'Uninstall preserves your personal data and artwork. If you skipped artwork, ' +
    'follow the offline artwork instructions in the release setup guide to add it without removing the app. This beta is unsigned.';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ArtworkPage.ID) and not WizardIsTaskSelected('downloadart');
end;

procedure AddArtworkFile(const Name, Hash: String);
begin
  DownloadPage.Add(ArtworkBaseURL + Name, Name, Hash);
end;

procedure CheckArtworkFile(const Name, Hash: String);
begin
  if not FileExists(AddBackslash(PartsDirectory) + Name) then
    RaiseException('An artwork release file is missing. Select the folder containing all six files.');
  if CompareText(GetSHA256OfFile(AddBackslash(PartsDirectory) + Name), Hash) <> 0 then
    RaiseException('An artwork release file failed verification. Existing installed artwork was preserved.');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  FreeBytes, TotalBytes: Int64;
begin
  Result := True;
  if (CurPageID <> wpReady) or not WizardIsTaskSelected('downloadart') then Exit;
  PartsDirectory := ArtworkPage.Values[0];
  if PartsDirectory = '' then begin
    if not GetSpaceOnDisk64(ExpandConstant('{tmp}'), FreeBytes, TotalBytes) or
       (FreeBytes < Int64(ArtworkBytes) * 2 + 536870912) then begin
      SuppressibleMsgBox('Downloading and assembling artwork needs about 4.7 GB of free working space. ' +
        'Free space or select existing parts; you can also return to Tasks and install the app without artwork.', mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    PartsDirectory := ExpandConstant('{tmp}');
    DownloadPage.Clear;
    AddArtworkFile('emoji-assets.parts.json', '41edeb112a6c497c39d9c2a0601925347aa3bdbb341d5030656d39cf0505a248');
    AddArtworkFile('emoji-assets.pack.part-001', 'a8b8760854f0d6c3ab6e4018fb72e3858bafcd32bd3b1ad9bae0697104d321a1');
    AddArtworkFile('emoji-assets.pack.part-002', '10e27dc1b7688f8881b9e055de0b6a2f01f70a1a96126221ae38688b71ab9b66');
    AddArtworkFile('emoji-assets.pack.part-003', '03078bfa3addbb7be7fda4dd2d06ef48cf600680a6e602ff4939d7f29bb2071e');
    AddArtworkFile('emoji-assets.pack.part-004', '08c2ffae5c497ba8352ddb1fe409b94f5723761e78d7f4e0c03d58d21c35418d');
    AddArtworkFile('emoji-assets.pack.part-005', '051c9dd5785e65249ad30e6fac66044216cfb3af10f09d9c07ca40ad075937a0');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        if not DownloadPage.AbortedByUser then
          SuppressibleMsgBox('Artwork download or verification failed. Check your connection and click Install to retry, ' +
            'or return to Tasks to install the app without artwork.', mbError, MB_OK, IDOK);
        Result := False;
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
  if Result then begin
    try
      CheckArtworkFile('emoji-assets.parts.json', '41edeb112a6c497c39d9c2a0601925347aa3bdbb341d5030656d39cf0505a248');
      CheckArtworkFile('emoji-assets.pack.part-001', 'a8b8760854f0d6c3ab6e4018fb72e3858bafcd32bd3b1ad9bae0697104d321a1');
      CheckArtworkFile('emoji-assets.pack.part-002', '10e27dc1b7688f8881b9e055de0b6a2f01f70a1a96126221ae38688b71ab9b66');
      CheckArtworkFile('emoji-assets.pack.part-003', '03078bfa3addbb7be7fda4dd2d06ef48cf600680a6e602ff4939d7f29bb2071e');
      CheckArtworkFile('emoji-assets.pack.part-004', '08c2ffae5c497ba8352ddb1fe409b94f5723761e78d7f4e0c03d58d21c35418d');
      CheckArtworkFile('emoji-assets.pack.part-005', '051c9dd5785e65249ad30e6fac66044216cfb3af10f09d9c07ca40ad075937a0');
    except
      SuppressibleMsgBox(GetExceptionMessage, mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Command: String;
begin
  if CurStep <> ssPostInstall then Exit;
  Command := ExpandConstant('{app}\payload\openclank.exe');
  if not Exec(Command, 'engine verify --json', ExpandConstant('{app}\payload'), SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
    RaiseException('The installed application files failed verification. Download a fresh installer and try again.');
  if WizardIsTaskSelected('downloadart') then begin
    if not Exec(Command, 'assets assemble --parts "' + AddBackslash(PartsDirectory) + '."',
      ExpandConstant('{app}\payload'), SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      RaiseException('Complete offline artwork assembly failed. Existing installed artwork was preserved. ' +
        'Check free disk space and the release files, then follow the offline artwork instructions in the release setup guide to add it without removing the app.');
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if DirExists(ExpandConstant('{app}\payload')) then
    Result := 'A payload already exists at this location. Uninstall the previous beta first or choose a new location. Personal data and artwork are preserved on uninstall.';
end;
