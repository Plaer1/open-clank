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
#ifndef SetupCredits
  #error SetupCredits must point to the release notice directory
#endif
#define ThemeArt "..\static\brand\open-clank\installer\windows"
#if Target == "windows-arm64"
  #define AllowedArchitecture "arm64"
#elif Target == "windows-x64"
  #define AllowedArchitecture "x64os"
#else
  #error Unsupported native payload target
#endif

[Setup]
AppId=OpenClank.Beta1.{#Target}
AppName=Open Clank Beta 3 ({#Target})
AppVersion={#AppVersion}
AppVerName=Open Clank {#AppVersion} Beta 3 ({#Target})
AppPublisher=Open Clank
AppPublisherURL=https://github.com/Plaer1/open-clank
DefaultDirName={localappdata}\Programs\Open Clank Beta\{#Target}
DefaultGroupName=Open Clank Beta ({#Target})
PrivilegesRequired=lowest
ArchitecturesAllowed={#AllowedArchitecture}
ArchitecturesInstallIn64BitMode={#AllowedArchitecture}
SetupArchitecture=x64
MinVersion=10.0.22000
UninstallDisplayIcon={app}\open-clank.ico
SetupIconFile=..\static\icons\open-clank-setup.ico
UninstallFilesDir={app}
OutputDir={#OutputRoot}
OutputBaseFilename=Open-Clank-{#AppVersion}-{#Target}-Setup
Compression=lzma2/fast
SolidCompression=no
LicenseFile={#SetupCredits}\terms.rtf
InfoBeforeFile={#SetupCredits}\notice-0.rtf
DisableWelcomePage=no
WizardStyle=modern dark hidebevels includetitlebar
WizardBackColor=#25272c
WizardSmallImageFile=
WizardImageFile=
WizardSizePercent=108
DefaultDialogFontName=Comic Clank
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
AlwaysRestart=no
SignedUninstaller=no
DisableProgramGroupPage=no
UsePreviousAppDir=no
UsePreviousTasks=no
SetupLogging=yes

[Messages]
ButtonNext=&Continue
ButtonBack=&Back
WizardLicense=Terms of use
LicenseLabel=Review before using Open Clank.
LicenseLabel3=
WizardInfoBefore=Licenses & credits
InfoBeforeLabel=Read the acknowledgments and included notices.
ClickNext=Choose Continue to proceed.
FinishedHeadingLabel=Open Clank is ready

[Tasks]
Name: "downloadart"; Description: "Install complete offline emoji artwork (download about 449 MB, or use existing release parts)"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#ThemeArt}\*.ttf"; Flags: dontcopy
Source: "{#ThemeArt}\*.png"; Flags: dontcopy
Source: "..\static\icons\open-clank-appimage-eye.png"; Flags: dontcopy
Source: "..\static\brand\open-clank\installer\terms.txt"; Flags: dontcopy
Source: "{#SetupCredits}\notice-list.txt"; Flags: dontcopy
Source: "{#SetupCredits}\notice-*.rtf"; Flags: dontcopy
Source: "..\static\icons\open-clank-setup.ico"; DestDir: "{app}"; DestName: "open-clank.ico"; Flags: ignoreversion
Source: "{#BundleRoot}\*"; DestDir: "{app}\payload"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Open Clank"; Filename: "{app}\payload\openclank.exe"; IconFilename: "{app}\open-clank.ico"; Parameters: "server start --open-browser"; WorkingDir: "{app}\payload"
Name: "{group}\Stop Open Clank"; Filename: "{app}\payload\openclank.exe"; Parameters: "server stop"; WorkingDir: "{app}\payload"
Name: "{group}\Uninstall Open Clank"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Open Clank Beta ({#Target})"; Filename: "{app}\payload\openclank.exe"; IconFilename: "{app}\open-clank.ico"; Parameters: "server start --open-browser"; WorkingDir: "{app}\payload"; Tasks: desktopicon

[Code]
const
  ArtworkBaseURL = 'https://github.com/Plaer1/open-clank/releases/download/v1.0.2-beta.1-artwork-compact/';
  ArtworkBytes = 449189888;
var
  ArtworkPage: TInputQueryWizardPage;
  ArtworkBrowseButton: TNewButton;
  DownloadPage: TDownloadWizardPage;
  PartsDirectory: String;
  RailSteps: array[0..3] of TNewStaticText;
  CreditsList: TNewComboBox;
  TitlePanel: TPanel;
  Stripes: array[0..5] of TPanel;
  FrameHandles: array[0..4] of HWND;
  FrameCallbacks: array[0..4] of NativeUInt;



procedure BrowseArtworkParts(Sender: TObject);
var
  Directory: String;
begin
  Directory := ArtworkPage.Values[0];
  if BrowseForFolder('Select the folder containing the artwork release files', Directory, False) then
    ArtworkPage.Values[0] := Directory;
end;

function ReleaseCapture: Boolean;
  external 'ReleaseCapture@user32.dll stdcall';
function DefSubclassProc(Wnd: HWND; Msg: Cardinal; WParam: NativeUInt; LParam: NativeInt): NativeInt;
  external 'DefSubclassProc@comctl32.dll stdcall';
function SetWindowSubclass(Wnd: HWND; Callback, Id, Data: NativeUInt): Boolean;
  external 'SetWindowSubclass@comctl32.dll stdcall';

function RemoveWindowSubclass(Wnd: HWND; Callback, Id: NativeUInt): Boolean;
  external 'RemoveWindowSubclass@comctl32.dll stdcall';

procedure HookFrame(Wnd: HWND; Callback, Id: NativeUInt);
begin
  if SetWindowSubclass(Wnd,Callback,Id,0) then begin
    FrameHandles[Id] := Wnd;
    FrameCallbacks[Id] := Callback;
  end;
end;

procedure UnhookFrame(Id: NativeUInt);
begin
  if FrameHandles[Id] <> 0 then begin
    RemoveWindowSubclass(FrameHandles[Id],FrameCallbacks[Id],Id);
    FrameHandles[Id] := 0;
  end;
end;

function AddFontResourceEx(FileName: String; Flags: Cardinal; Reserved: NativeInt): Integer;
  external 'AddFontResourceExW@gdi32.dll stdcall';
function RemoveFontResourceEx(FileName: String; Flags: Cardinal; Reserved: NativeInt): Boolean;
  external 'RemoveFontResourceExW@gdi32.dll stdcall';
function ShowWindow(Handle: HWND; Command: Integer): Boolean;
  external 'ShowWindow@user32.dll stdcall';
function IsZoomed(Handle: HWND): Boolean;
  external 'IsZoomed@user32.dll stdcall';
function GetSystemMetrics(Index: Integer): Integer;
  external 'GetSystemMetrics@user32.dll stdcall';

procedure RegisterFace(Name: String);
begin
  ExtractTemporaryFile(Name);
  AddFontResourceEx(ExpandConstant('{tmp}\') + Name, 16, 0);
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if WizardSilent then Exit;
  RegisterFace('ComicClank-Regular.ttf');
  RegisterFace('ComicClank-Bold.ttf');
  RegisterFace('ComicClankMono-Regular.ttf');
end;

procedure DeinitializeSetup;
var I: Integer;
begin
  if WizardSilent then Exit;
  for I := 0 to 4 do UnhookFrame(I);
  RemoveFontResourceEx(ExpandConstant('{tmp}\ComicClank-Regular.ttf'), 16, 0);
  RemoveFontResourceEx(ExpandConstant('{tmp}\ComicClank-Bold.ttf'), 16, 0);
  RemoveFontResourceEx(ExpandConstant('{tmp}\ComicClankMono-Regular.ttf'), 16, 0);
end;

procedure ToggleMax(Sender: TObject);
begin
  if IsZoomed(WizardForm.Handle) then ShowWindow(WizardForm.Handle, 9)
  else ShowWindow(WizardForm.Handle, 3);
end;

procedure MinimizeSetup(Sender: TObject);
begin
  ShowWindow(WizardForm.Handle, 6);
end;

function TitleInput(Wnd: HWND; Msg: Cardinal; WParam: NativeUInt; LParam: NativeInt;
  Id, Data: NativeUInt): NativeInt;
begin
  if Msg = $0082 then UnhookFrame(Id);
  if Msg = $0201 then begin
    ReleaseCapture;
    SendMessage(WizardForm.Handle, $00A1, 2, 0);
    Result := 0;
  end else
    Result := DefSubclassProc(Wnd, Msg, WParam, LParam);
end;

procedure CloseSetup(Sender: TObject);
begin
  WizardForm.Close;
end;

function Plate(Parent: TWinControl; Fill: TColor): TPanel;
begin
  Result := TPanel.Create(Parent);
  Result.Parent := Parent;
  Result.BevelOuter := bvNone;
  Result.StyleElements := [];
  Result.ParentBackground := False;
  Result.Color := Fill;
  Result.Caption := '';
end;

function LabelOn(Parent: TWinControl; Text: String; X,Y,W,H: Integer; Ink: TColor): TNewStaticText;
begin
  Result := TNewStaticText.Create(Parent);
  Result.Parent := Parent;
  Result.AutoSize := False;
  Result.StyleElements := [];
  Result.Font.Name := 'Comic Clank';
  Result.Font.Color := Ink;
  Result.ShowAccelChar := False;
  Result.Caption := Text;
  Result.SetBounds(X,Y,W,H);
end;

procedure PutArt(Parent: TWinControl; Name: String; X,Y,W,H: Integer; Fill: TColor);
var
  Art: TBitmapImage;
begin
  ExtractTemporaryFile(Name);
  Art := TBitmapImage.Create(Parent);
  Art.Parent := Parent;
  Art.BackColor := Fill;
  Art.Stretch := True;
  Art.PngImage.LoadFromFile(ExpandConstant('{tmp}\') + Name);
  Art.SetBounds(X,Y,W,H);
end;

function FrameButton(Text, Hint: String; Offset: Integer; Action: TNotifyEvent): TNewButton;
begin
  Result := TNewButton.Create(TitlePanel);
  Result.Parent := TitlePanel;
  Result.SetBounds(TitlePanel.Width-ScaleX(Offset),ScaleY(3),ScaleX(30),TitlePanel.Height-ScaleY(9));
  Result.Anchors := [akTop,akRight];
  Result.Caption := Text;
  Result.Font.Name := 'Segoe UI Symbol';
  Result.Font.Size := 12;
  Result.Hint := Hint;
  Result.ShowHint := True;
  Result.OnClick := Action;
end;

procedure PositionSpectrum;
var I, L: Integer;
    Stops: array[0..6] of Integer;
begin
  Stops[0] := 0; Stops[1] := 82; Stops[2] := 86; Stops[3] := 90;
  Stops[4] := 93; Stops[5] := 97; Stops[6] := 100;
  for I := 0 to 5 do begin
    L := TitlePanel.Width*Stops[I] div 100;
    Stripes[I].SetBounds(L, TitlePanel.Height-ScaleY(3),
      TitlePanel.Width*Stops[I+1] div 100-L, ScaleY(3));
  end;
end;

function FrameResize(Wnd: HWND; Msg: Cardinal; WParam: NativeUInt; LParam: NativeInt;
  Id, Data: NativeUInt): NativeInt;
begin
  if Msg = $0082 then UnhookFrame(Id);
  Result := DefSubclassProc(Wnd, Msg, WParam, LParam);
  if Msg = 5 then PositionSpectrum;
end;

function GetDC(Wnd: HWND): HDC; external 'GetDC@user32.dll stdcall';
function ReleaseDC(Wnd: HWND; DC: HDC): Integer; external 'ReleaseDC@user32.dll stdcall';
function CreateSolidBrush(Fill: TColor): NativeUInt; external 'CreateSolidBrush@gdi32.dll stdcall';
function CreatePen(Style, Width: Integer; Ink: TColor): NativeUInt; external 'CreatePen@gdi32.dll stdcall';
function SelectObject(DC: HDC; Obj: NativeUInt): NativeUInt; external 'SelectObject@gdi32.dll stdcall';
function DeleteObject(Obj: NativeUInt): Boolean; external 'DeleteObject@gdi32.dll stdcall';
function PaintRect(DC: HDC; var R: TRect; Brush: NativeUInt): Integer; external 'FillRect@user32.dll stdcall';
function RoundedRect(DC: HDC; L,T,R,B,RX,RY: Integer): Boolean; external 'RoundRect@gdi32.dll stdcall';
function TextColor(DC: HDC; Ink: TColor): TColor; external 'SetTextColor@gdi32.dll stdcall';
function SetBkMode(DC: HDC; Mode: Integer): Integer; external 'SetBkMode@gdi32.dll stdcall';
function DrawButtonText(DC: HDC; Text: String; Count: Integer; var R: TRect; Flags: Cardinal): Integer;
  external 'DrawTextW@user32.dll stdcall';

{ Paint the existing button so keyboard activation, focus and disabled states remain native. }
function PrimaryPaint(Wnd: HWND; Msg: Cardinal; WParam: NativeUInt; LParam: NativeInt;
  Id, Data: NativeUInt): NativeInt;
var DC: HDC; R: TRect; Fill, Ink, Edge: TColor;
    Bg, Brush, Pen, PrevBrush, PrevPen, PrevFont: NativeUInt;
begin
  if Msg = $0082 then UnhookFrame(Id);
  Result := DefSubclassProc(Wnd, Msg, WParam, LParam);
  if (Msg <> 15) and (Msg <> $0318) then Exit;
  if Msg = 15 then DC := GetDC(Wnd) else DC := WParam;
  if DC = 0 then Exit;
  R.Left := 0; R.Top := 0; R.Right := WizardForm.NextButton.Width; R.Bottom := WizardForm.NextButton.Height;
  Fill := $48bef6; Ink := $120f0e; Edge := $120f0e;
  if not WizardForm.NextButton.Enabled then begin Fill := $3e3734; Ink := $b0bfc5; end
  else if (SendMessage(Wnd,$F2,0,0) and 4) <> 0 then Fill := $429ed3;
  if WizardForm.NextButton.Focused then Edge := $e8c762;
  Bg := CreateSolidBrush($2c2725);
  PaintRect(DC,R,Bg);
  DeleteObject(Bg);
  Brush := CreateSolidBrush(Fill); Pen := CreatePen(0,ScaleX(1),Edge);
  PrevBrush := SelectObject(DC,Brush); PrevPen := SelectObject(DC,Pen);
  RoundedRect(DC,0,0,R.Right,R.Bottom,ScaleX(9),ScaleY(9));
  PrevFont := SelectObject(DC,WizardForm.NextButton.Font.Handle);
  TextColor(DC,Ink); SetBkMode(DC,1);
  DrawButtonText(DC,WizardForm.NextButton.Caption,-1,R,$25);
  SelectObject(DC,PrevFont); SelectObject(DC,PrevPen); SelectObject(DC,PrevBrush);
  DeleteObject(Pen); DeleteObject(Brush);
  if Msg = 15 then ReleaseDC(Wnd,DC);
end;

procedure ApplyClankFrame;
var
  Rail, FooterLine: TPanel;
  Version, TitleText: TNewStaticText;
  Colors: array[0..5] of TColor;
  I,H,W,Extra,ArtW: Integer;
begin
  H := ScaleY(36);
  W := ScaleX(132);
  Extra := GetSystemMetrics(0)-WizardForm.ClientWidth-ScaleX(16);
  if Extra < 0 then Extra := 0;
  if Extra > W then Extra := W;
  WizardForm.BorderStyle := bsNone;
  WizardForm.ClientWidth := WizardForm.ClientWidth + Extra;
  WizardForm.ClientHeight := WizardForm.ClientHeight + H;
  WizardForm.OuterNotebook.SetBounds(W,H,WizardForm.ClientWidth-W,WizardForm.Bevel.Top-H);
  WizardForm.Font.Name := 'Comic Clank';
  WizardForm.Font.Color := $d6f4ff;
  I := WizardForm.NextButton.Left;
  WizardForm.NextButton.Left := WizardForm.CancelButton.Left;
  WizardForm.CancelButton.Left := I;
  WizardForm.NextButton.Font.Name := 'Comic Clank';
  WizardForm.NextButton.Font.Style := [fsBold];
  HookFrame(WizardForm.NextButton.Handle, CreateCallback(@PrimaryPaint), 0);
  TitlePanel := Plate(WizardForm, $3e3734);
  TitlePanel.SetBounds(0,0,WizardForm.ClientWidth,H);
  TitlePanel.Anchors := [akLeft,akRight,akTop];
  PutArt(TitlePanel,'triangle.png',ScaleX(8),ScaleY(4),ScaleX(26),ScaleY(26),TitlePanel.Color);
  TitleText := LabelOn(TitlePanel,'Open Clank Setup',ScaleX(39),ScaleY(6),ScaleX(170),ScaleY(26),$48bef6);
  TitleText.Font.Style := [fsBold];
  TitleText.Font.Size := 11;
  Version := LabelOn(TitlePanel,'Beta 3 · Windows',TitlePanel.Width-ScaleX(234),ScaleY(8),ScaleX(132),ScaleY(21),$b0bfc5);
  Version.Font.Name := 'Comic Clank Mono';
  Version.Alignment := taRightJustify;
  Version.Anchors := [akTop,akRight];
  HookFrame(TitlePanel.Handle, CreateCallback(@TitleInput), 1);
  HookFrame(TitleText.Handle, CreateCallback(@TitleInput), 2);
  HookFrame(Version.Handle, CreateCallback(@TitleInput), 3);
  FrameButton('−','Minimize',97,@MinimizeSetup);
  FrameButton('□','Maximize or restore',65,@ToggleMax);
  FrameButton('×','Close setup',33,@CloseSetup);
  Colors[0] := $48bef6; Colors[1] := $e8c762; Colors[2] := $53dea8;
  Colors[3] := $6e77ff; Colors[4] := $b06aed; Colors[5] := $e8a7b7;
  for I := 0 to 5 do Stripes[I] := Plate(TitlePanel, Colors[I]);
  PositionSpectrum;
  HookFrame(TitlePanel.Handle, CreateCallback(@FrameResize), 4);
  FooterLine := Plate(WizardForm,$625a55);
  FooterLine.SetBounds(0,WizardForm.Bevel.Top,WizardForm.ClientWidth,ScaleY(1));
  FooterLine.Anchors := [akLeft,akRight,akBottom];
  Rail := Plate(WizardForm,$262220);
  Rail.SetBounds(0,H,W,WizardForm.Bevel.Top-H);
  Rail.Anchors := [akLeft,akTop,akBottom];
  PutArt(Rail,'open-clank-appimage-eye.png',(W-ScaleX(82)) div 2,ScaleY(18),ScaleX(82),ScaleY(82),Rail.Color);
  for I := 0 to 3 do begin
    RailSteps[I] := LabelOn(Rail,'',ScaleX(12),ScaleY(113+I*39),W-ScaleX(18),ScaleY(38),$b0bfc5);
    RailSteps[I].WordWrap := True;
    RailSteps[I].Font.Size := 10;
  end;
  WizardForm.WizardBitmapImage.Hide;
  WizardForm.WizardBitmapImage2.Hide;
  WizardForm.WizardSmallBitmapImage.Hide;
  WizardForm.PageNameLabel.Font.Name := 'Comic Clank';
  WizardForm.PageNameLabel.Font.Size := 15;
  WizardForm.PageNameLabel.Font.Style := [fsBold];
  WizardForm.PageNameLabel.Height := ScaleY(28);
  WizardForm.PageNameLabel.Top := ScaleY(6);
  WizardForm.PageDescriptionLabel.Top := WizardForm.PageNameLabel.Top+WizardForm.PageNameLabel.Height+ScaleY(1);
  WizardForm.WelcomeLabel1.Hide;
  WizardForm.WelcomeLabel2.SetBounds(ScaleX(27),ScaleY(134),WizardForm.WelcomePage.Width-ScaleX(54),ScaleY(95));
  WizardForm.WelcomeLabel2.Font.Name := 'Comic Clank';
  WizardForm.WelcomeLabel2.Font.Size := 12;
  WizardForm.WelcomeLabel2.StyleElements := [seClient,seBorder];
  WizardForm.WelcomeLabel2.Font.Color := $d6f4ff;
  WizardForm.WelcomeLabel2.Caption := 'Install Open Clank for this Windows account. You can choose a destination, shortcuts and offline artwork before installing.';
  ArtW := WizardForm.WelcomePage.Width-ScaleX(60);
  if ArtW > ScaleX(385) then ArtW := ScaleX(385);
  PutArt(WizardForm.WelcomePage,'wordmark.png',ScaleX(26),ScaleY(29),ArtW,ArtW*306 div 1600,$2c2725);
  WizardForm.FinishedHeadingLabel.Left := ScaleX(27);
  WizardForm.FinishedHeadingLabel.Width := WizardForm.FinishedPage.Width-ScaleX(54);
  WizardForm.FinishedLabel.Left := WizardForm.FinishedHeadingLabel.Left;
  WizardForm.FinishedLabel.Width := WizardForm.FinishedHeadingLabel.Width;
end;

procedure SelectNotice(Sender: TObject);
var Body: AnsiString;
    Name: String;
begin
  Name := 'notice-'+IntToStr(CreditsList.ItemIndex)+'.rtf';
  if not FileExists(ExpandConstant('{tmp}\')+Name) then ExtractTemporaryFile(Name);
  if not LoadStringFromFile(ExpandConstant('{tmp}\')+Name,Body) then
    RaiseException('Unable to open the selected notice.');
  WizardForm.InfoBeforeMemo.RTFText := Body;
end;

procedure CreateTermsReader;
var Terms: TNewMemo;
begin
  ExtractTemporaryFile('terms.txt');
  Terms := TNewMemo.Create(WizardForm.LicensePage);
  Terms.Parent := WizardForm.LicensePage;
  Terms.SetBounds(WizardForm.LicenseMemo.Left,WizardForm.LicenseMemo.Top,
    WizardForm.LicenseMemo.Width,WizardForm.LicenseMemo.Height);
  Terms.Anchors := [akLeft,akTop,akRight,akBottom];
  Terms.StyleElements := [];
  Terms.Color := $2c2725;
  Terms.Font.Name := 'Comic Clank';
  Terms.Font.Size := 11;
  Terms.Font.Color := $d6f4ff;
  Terms.ReadOnly := True;
  Terms.WordWrap := True;
  Terms.ScrollBars := ssVertical;
  Terms.Lines.LoadFromFile(ExpandConstant('{tmp}\terms.txt'));
  Terms.TabOrder := WizardForm.LicenseMemo.TabOrder;
  WizardForm.LicenseMemo.Hide;
end;

procedure CreateNoticeReader;
var Names: TArrayOfString;
    I, Y: Integer;
begin
  ExtractTemporaryFile('notice-list.txt');
  if not LoadStringsFromFile(ExpandConstant('{tmp}\notice-list.txt'),Names) then
    RaiseException('The setup notice index could not be read.');
  WizardForm.InfoBeforeClickLabel.Hide;
  CreditsList := TNewComboBox.Create(WizardForm.InfoBeforePage);
  CreditsList.Parent := WizardForm.InfoBeforePage;
  CreditsList.Style := csDropDownList;
  CreditsList.SetBounds(WizardForm.InfoBeforeMemo.Left,0,WizardForm.InfoBeforeMemo.Width,ScaleY(28));
  CreditsList.Anchors := [akLeft,akRight,akTop];
  for I := 0 to GetArrayLength(Names)-1 do begin
    if Names[I]='ACKNOWLEDGMENTS.md' then CreditsList.Items.Add('Acknowledgments')
    else if Names[I]='LICENSE' then CreditsList.Items.Add('Open Clank · AGPL-3.0-or-later')
    else CreditsList.Items.Add(Names[I]);
  end;
  CreditsList.ItemIndex := 0;
  CreditsList.OnChange := @SelectNotice;
  Y := CreditsList.Top+CreditsList.Height+ScaleY(8);
  WizardForm.InfoBeforeMemo.SetBounds(CreditsList.Left,Y,CreditsList.Width,WizardForm.InfoBeforePage.Height-Y);
  SelectNotice(nil);
end;

procedure CurPageChanged(CurPageID: Integer);
var Names: array[0..3] of String;
    Active,I: Integer;
begin
  if WizardSilent then Exit;
  Names[0] := 'Welcome'; Names[1] := 'Terms of use'; Names[2] := 'Licenses & credits'; Names[3] := 'Install';
  if CurPageID=wpWelcome then Active := 0
  else if CurPageID=wpLicense then Active := 1
  else if CurPageID=wpInfoBefore then Active := 2
  else Active := 3;
  for I := 0 to 3 do begin
    if I = Active then begin
      RailSteps[I].Caption := '◆  '+Names[I];
      RailSteps[I].Font.Color := $48bef6;
      RailSteps[I].Font.Style := [fsBold];
    end else begin
      if I < Active then RailSteps[I].Caption := '✓  '+Names[I]
      else RailSteps[I].Caption := '○  '+Names[I];
      RailSteps[I].Font.Color := $b0bfc5;
      RailSteps[I].Font.Style := [];
    end;
  end;
end;

procedure InitializeWizard;
begin
  if not WizardSilent then begin
    ApplyClankFrame;
    CreateTermsReader;
    CreateNoticeReader;
  end;
  ArtworkPage := CreateInputQueryPage(wpSelectTasks, 'Offline emoji artwork',
    'Download the complete artwork or use existing release files',
    'Leave the folder empty to download the verified release artwork with progress. ' +
    'If you already have emoji-assets.pack.part-001 and emoji-assets.parts.json, select their folder. ' +
    'Artwork stays in your personal application data and is preserved on uninstall. ' +
    'This beta installer is unsigned.');
  ArtworkPage.Add('Existing artwork parts folder (optional):', False);
  ArtworkBrowseButton := TNewButton.Create(ArtworkPage);
  ArtworkBrowseButton.Parent := ArtworkPage.Surface;
  ArtworkBrowseButton.Caption := 'Browse...';
  ArtworkBrowseButton.Left := ArtworkPage.SurfaceWidth - ScaleX(85);
  ArtworkBrowseButton.Top := ArtworkPage.Edits[0].Top - ScaleY(1);
  ArtworkBrowseButton.Width := ScaleX(85);
  ArtworkBrowseButton.Height := WizardForm.NextButton.Height;
  ArtworkBrowseButton.OnClick := @BrowseArtworkParts;
  ArtworkPage.Edits[0].Width := ArtworkBrowseButton.Left - ScaleX(10);
  ArtworkPage.Values[0] := ExpandConstant('{param:ARTWORKPARTS|}');
  DownloadPage := CreateDownloadPage('Downloading offline emoji artwork',
    'Downloading about 449 MB from the published Open Clank Beta release; verifying every file.', nil);
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
    RaiseException('An artwork release file is missing. Select the folder containing the part and its manifest.');
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
      SuppressibleMsgBox('Downloading and assembling artwork needs about 1.5 GB of free working space. ' +
        'Free space or select existing parts; you can also return to Tasks and install the app without artwork.', mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    PartsDirectory := ExpandConstant('{tmp}');
    DownloadPage.Clear;
    AddArtworkFile('emoji-assets.parts.json', 'f58411bc3aee12c7947ef27271f86cde9e4813df230c97d6394c1a715acdb2f9');
    AddArtworkFile('emoji-assets.pack.part-001', '42adf0a79e4012ae71be7f5b3c348b51cdbfddc254a02829547cb60a58a4074c');
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
      CheckArtworkFile('emoji-assets.parts.json', 'f58411bc3aee12c7947ef27271f86cde9e4813df230c97d6394c1a715acdb2f9');
      CheckArtworkFile('emoji-assets.pack.part-001', '42adf0a79e4012ae71be7f5b3c348b51cdbfddc254a02829547cb60a58a4074c');
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
