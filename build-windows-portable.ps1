#Requires -Version 5.1
param(
    [switch]$UseExistingVerifiedEngine,
    [ValidateSet('windows-x64','windows-arm64')][string]$Target = 'windows-x64',
    [string]$PythonExecutable,
    [string]$BunExecutable = 'bun',
    [string]$EngineInstallRoot,
    [string]$VerifiedRustArtifacts,
    [string]$VerifiedRustArtifactsSha256,
    [switch]$IncludeCopalRedb
)

<#
  Build a portable Windows target distribution with matching target Python.

  Output layout:
    dist\windows-<arch>\openclank\openclank.exe
    dist\windows-<arch>\openclank\_internal\libexec\openclank\engine\...
    dist\windows-<arch>\openclank\_internal\bin\helper-artifacts.json
    dist\windows-<arch>\openclank\portable-provenance.json
    dist\windows-<arch>\openclank\SHA256SUMS

  The app then keeps using its normal filesystem layout when frozen.

  Usage:
    .\build-windows-portable.ps1 -Target windows-x64
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""
    Write-Host ("ERROR: " + $msg) -ForegroundColor Red
    exit 1
}

function Write-Utf8NoBom($Path, $Text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

Write-Step "Checking for Python"
$pyExe = $PythonExecutable
if ($pyExe) {
    if (-not (Test-Path $pyExe -PathType Leaf)) { Fail "Explicit Python interpreter is missing." }
} elseif (Test-Path ".\.venv\Scripts\python.exe") {
    $pyExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
} else {
    foreach ($c in @("py", "python")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { $pyExe = $cmd.Source; break }
    }
    if ($pyExe -like "*WindowsApps*python.exe") {
        $pyCmd = Get-Command py -ErrorAction SilentlyContinue
        if ($pyCmd) {
            $pyExe = $pyCmd.Source
        }
    }
}
if (-not $pyExe) {
    Fail "Python not found on PATH. Install Python 3.11+ first."
}
Write-Host ("Using Python: " + $pyExe)

$hostTarget = (& $pyExe -c "import platform,struct,sys; print(f'{sys.platform}:{platform.machine().lower()}:{struct.calcsize(chr(80))*8}')").Trim()
$expectedPython = if ($Target -eq 'windows-arm64') { @('win32:arm64:64','win32:aarch64:64') } else { @('win32:amd64:64','win32:x86_64:64') }
if ($LASTEXITCODE -ne 0 -or $hostTarget -notin $expectedPython) {
    Fail "Portable $Target requires matching target Python and complete requirements (found $hostTarget)."
}
$rustTarget = if ($Target -eq 'windows-arm64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }

Write-Step "Installing build dependencies"
& $pyExe -m pip install --upgrade pip --quiet
& $pyExe -m pip install -r requirements.txt pyinstaller==6.16.0
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed." }

# Separate target engine pointer/output without copying the full source tree.
$sourceEngineRoot = Join-Path $PSScriptRoot "libexec\openclank\engine"
$engineRootArgs = @()
if ($EngineInstallRoot) {
    if ($EngineInstallRoot -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/?]+\\[^\\/?]+(?:\\|$)|\\\\\?\\[A-Za-z]:\\|\\\\\?\\UNC\\[^\\/?]+\\[^\\/?]+(?:\\|$))') { Fail 'Explicit EngineInstallRoot must be fully qualified; drive-relative/current-drive paths are refused.' }
    $sourceEngineRoot = [IO.Path]::GetFullPath($EngineInstallRoot)
    $engineRootArgs = @('--install-root', $sourceEngineRoot)
}
if ($UseExistingVerifiedEngine) {
    Write-Step "Verifying the supplied managed engine"
    & $pyExe scripts/openclank_engine.py @engineRootArgs verify --target $Target --json
} else {
    Write-Step "Building and ACP-verifying the exact managed engine"
    & $pyExe scripts/openclank_engine.py @engineRootArgs build --target $Target --bun $BunExecutable --json
}
if ($LASTEXITCODE -ne 0) { Fail "Managed engine build or verification failed." }

# Stage only current.json and the one activated versioned artifact. Copying the
# ambient libexec tree could silently ship stale engines from earlier builds.
# $sourceEngineRoot is the same root just verified above.
$sourceCurrentPath = Join-Path $sourceEngineRoot "current.json"
if (-not (Test-Path $sourceCurrentPath -PathType Leaf)) { Fail "Verified engine current pointer is missing." }
$sourceCurrent = Get-Content $sourceCurrentPath -Raw -Encoding UTF8 | ConvertFrom-Json
$sourceArtifactRelative = [string]$sourceCurrent.artifact
if (-not $sourceArtifactRelative -or [System.IO.Path]::IsPathRooted($sourceArtifactRelative) -or $sourceArtifactRelative -match '(^|[\\/])\.\.([\\/]|$)') {
    Fail "Verified engine current pointer contains an unsafe artifact path."
}
$sourceArtifact = Join-Path $sourceEngineRoot $sourceArtifactRelative.Replace("/", "\")
if (-not (Test-Path $sourceArtifact -PathType Container)) { Fail "Verified engine artifact is missing." }
$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("openclank-windows-portable-" + [guid]::NewGuid().ToString("N"))
$stageEngineRoot = Join-Path $stageRoot "libexec\openclank\engine"
$stageArtifact = Join-Path $stageEngineRoot $sourceArtifactRelative.Replace("/", "\")
New-Item -ItemType Directory -Path (Split-Path $stageArtifact -Parent) -Force | Out-Null
Copy-Item $sourceCurrentPath (Join-Path $stageEngineRoot "current.json") -Force
Copy-Item $sourceArtifact $stageArtifact -Recurse -Force

if (($VerifiedRustArtifacts -and -not $VerifiedRustArtifactsSha256) -or ($VerifiedRustArtifactsSha256 -and -not $VerifiedRustArtifacts)) { Fail "Both reviewed Rust adoption manifest and SHA256 required." }
Write-Step "Preparing target-specific native helpers"
$stageBin = Join-Path $stageRoot 'bin'
New-Item -ItemType Directory -Path $stageBin | Out-Null
$cargoTargetDir = Join-Path $PSScriptRoot ('build\helpers\' + $Target)
$helperCrates = @(
    @{ manifest='packages/odysseus-files/Cargo.toml'; bins=@('odysseus-files-service','odysseus-shell-thumbnail-helper') },
    @{ manifest='packages/openclank-history/Cargo.toml'; bins=@('openclank-history-service') },
    @{ manifest='mcp_servers/frankenmemory/Cargo.toml'; bins=@('fm-mcp') }
)
if ($IncludeCopalRedb) { Fail 'Copal redb packaging requires its verified build-identity metadata; use the loose-file release lane until that inventory is implemented.' }
if ($VerifiedRustArtifacts) {
    & $pyExe -B scripts/windows_helper_artifacts.py --target $Target --source-root $PSScriptRoot --bin-dir $stageBin --reuse-rust-manifest $VerifiedRustArtifacts --reuse-rust-sha256 $VerifiedRustArtifactsSha256 --output (Join-Path $stageRoot 'rust-reuse-receipt.json')
    if ($LASTEXITCODE -ne 0) { Fail 'Reviewed Rust artifact reuse failed; preserve staged state.' }
} else {
foreach ($crate in $helperCrates) {
    $cargoArgs = @('+1.99.0','build','--locked','--release','--jobs','2','--target',$rustTarget,'--target-dir',$cargoTargetDir,'--manifest-path',$crate.manifest)
    foreach ($bin in $crate.bins) { $cargoArgs += @('--bin',$bin) }
    & cargo @cargoArgs
    if ($LASTEXITCODE -ne 0) { Fail ('Helper target build failed: ' + $crate.manifest) }
    foreach ($bin in $crate.bins) {
        Copy-Item (Join-Path $cargoTargetDir ($rustTarget + '\release\' + $bin + '.exe')) $stageBin -ErrorAction Stop
    }
}
}
& $pyExe -m PyInstaller --noconfirm --clean --onefile --console --noupx --name openclank-windows-host-apps --paths . --distpath $stageBin --workpath (Join-Path $stageRoot 'host-apps-work') --specpath $stageRoot src/openclank/windows_host_apps.py
if ($LASTEXITCODE -ne 0) { Fail 'Host application helper build failed.' }
& $pyExe -m PyInstaller --noconfirm --clean --onefile --console --noupx --name openclank-windows-desktop-capture --paths . --collect-submodules winrt --collect-submodules PIL --distpath $stageBin --workpath (Join-Path $stageRoot 'desktop-work') --specpath $stageRoot src/windows_desktop_capture.py
if ($LASTEXITCODE -ne 0) { Fail 'Desktop capture/OCR helper build failed.' }
$adoptionArgs=@()
if ($VerifiedRustArtifacts) { $adoptionArgs=@('--adoption-provenance-sha256',$VerifiedRustArtifactsSha256) }
& $pyExe -B scripts/windows_helper_artifacts.py --target $Target --bin-dir $stageBin @adoptionArgs --output (Join-Path $stageBin 'helper-artifacts.json')
if ($LASTEXITCODE -ne 0) { Fail 'Helper PE/source/hash inventory failed.' }

Write-Step "Building portable exe bundle"
$targetDist = Join-Path $PSScriptRoot ('dist\' + $Target)
if (Test-Path (Join-Path $targetDist 'openclank')) { Fail 'Target bundle already exists; preserve it and choose a fresh build location before rebuilding.' }

$dataArgs = @(
    "--add-data", "static;static",
    "--add-data", "scripts;scripts",
    "--add-data", "mcp_servers;mcp_servers",
    "--add-data", "services/hwfit/data;services/hwfit/data",
    "--add-data", "config;config",
    "--add-data", "contracts;contracts",
    "--add-data", ((Join-Path $stageRoot "libexec") + ";libexec")
    "--add-data", ($stageBin + ";bin")
)

$pyInstallerExit = 1
try {
    & $pyExe -m PyInstaller --noconfirm --clean --onedir --console --noupx --distpath $targetDist --workpath (Join-Path $stageRoot 'app-work') --specpath $stageRoot --contents-directory _internal --icon=static/icon.ico --name openclank --hidden-import=app --collect-submodules=keyring.backends @dataArgs openclank_entry.py
    $pyInstallerExit = $LASTEXITCODE
} finally {
    Remove-Item -Recurse -Force $stageRoot -ErrorAction SilentlyContinue
}
if ($pyInstallerExit -ne 0) { Fail "PyInstaller build failed." }

$bundleRoot = Join-Path $targetDist 'openclank'
$publicExe = Join-Path $bundleRoot "openclank.exe"
if (-not (Test-Path $publicExe)) { Fail "Portable public command is missing." }

$engineRoot = Join-Path $bundleRoot "_internal\libexec\openclank\engine"
$engineCurrentPath = Join-Path $engineRoot "current.json"
if (-not (Test-Path $engineCurrentPath -PathType Leaf)) { Fail "Packaged engine current pointer is missing." }
$engineCurrent = Get-Content $engineCurrentPath -Raw -Encoding UTF8 | ConvertFrom-Json
$artifactRelative = [string]$engineCurrent.artifact
if (-not $artifactRelative -or [System.IO.Path]::IsPathRooted($artifactRelative) -or $artifactRelative -match '(^|[\\/])\.\.([\\/]|$)') {
    Fail "Packaged engine current pointer contains an unsafe artifact path."
}
$engineProvenancePath = Join-Path $engineRoot (Join-Path $artifactRelative.Replace("/", "\") "provenance.json")
if (-not (Test-Path $engineProvenancePath -PathType Leaf)) { Fail "Packaged engine provenance is missing." }
$engineProvenance = Get-Content $engineProvenancePath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$engineProvenance.target -ne $Target) { Fail "Packaged engine target disagrees with portable target." }
$engineBinaryPath = Join-Path (Split-Path $engineProvenancePath -Parent) ([string]$engineProvenance.binary.name)
if (-not (Test-Path $engineBinaryPath -PathType Leaf)) { Fail "Packaged engine binary is missing." }

$relativeEngineProvenance = $engineProvenancePath.Substring($bundleRoot.Length + 1).Replace("\", "/")
$relativeEngineBinary = $engineBinaryPath.Substring($bundleRoot.Length + 1).Replace("\", "/")
$appVersion = (& $pyExe -c "from src.constants import APP_VERSION; print(APP_VERSION)").Trim()
$portableProvenance = [ordered]@{
    schema_version = 2
    product = "Open Clank"
    artifact_kind = ($Target + '-portable')
    version = $appVersion
    target = $Target
    public_entrypoint = "openclank.exe"
    contents_directory = "_internal"
    helpers = [ordered]@{
        inventory_path = '_internal/bin/helper-artifacts.json'
        inventory_sha256 = (Get-FileHash (Join-Path $bundleRoot '_internal\bin\helper-artifacts.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    engine = [ordered]@{
        current_path = "_internal/libexec/openclank/engine/current.json"
        provenance_path = $relativeEngineProvenance
        provenance_sha256 = (Get-FileHash $engineProvenancePath -Algorithm SHA256).Hash.ToLowerInvariant()
        binary_path = $relativeEngineBinary
        binary_sha256 = (Get-FileHash $engineBinaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
        source_sha256 = [string]$engineProvenance.inputs.source_sha256
        vendor_manifest_sha256 = [string]$engineProvenance.inputs.vendor_manifest_sha256
        managed_schema_sha256 = [string]$engineProvenance.managed_schema.sha256
        protocols = $engineProvenance.protocols
    }
}
$portableJson = ($portableProvenance | ConvertTo-Json -Depth 8) + "`n"
Write-Utf8NoBom (Join-Path $bundleRoot "portable-provenance.json") $portableJson

$checksumLines = Get-ChildItem $bundleRoot -Recurse -File |
    Where-Object { $_.Name -ne "SHA256SUMS" } |
    Sort-Object FullName |
    ForEach-Object {
        $relative = $_.FullName.Substring($bundleRoot.Length + 1).Replace("\", "/")
        $digest = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$digest  $relative"
    }
$checksumLines | Set-Content (Join-Path $bundleRoot "SHA256SUMS") -Encoding ASCII

Write-Step "Verifying the checksummed portable command and managed engine"
$versionOutput = (& $publicExe --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $versionOutput -ne "Open Clank $appVersion") {
    Fail "Packaged Open Clank command failed its version smoke test."
}
& $publicExe engine verify --json
if ($LASTEXITCODE -ne 0) { Fail "Packaged managed engine or portable payload failed verification." }

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "Portable app folder: $bundleRoot" -ForegroundColor Green
Write-Host "Run openclank.exe for the TUI; distribute and sign the whole checksummed folder." -ForegroundColor Green
