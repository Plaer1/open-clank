#Requires -Version 5.1
param(
    [switch]$UseExistingVerifiedEngine
)

<#
  Build a portable Windows x64 distribution for Open Clank.

  Output layout:
    dist\openclank\openclank.exe
    dist\openclank\_internal\libexec\openclank\engine\...
    dist\openclank\portable-provenance.json
    dist\openclank\SHA256SUMS

  The app then keeps using its normal filesystem layout when frozen.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\build-windows-portable.ps1
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
$pyExe = $null
if (Test-Path ".\.venv\Scripts\python.exe") {
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
if ($LASTEXITCODE -ne 0 -or $hostTarget -notin @("win32:amd64:64", "win32:x86_64:64")) {
    Fail "The Tier-1 portable artifact must be built with 64-bit x64 Python on Windows (found $hostTarget)."
}

Write-Step "Installing build dependencies"
& $pyExe -m pip install --upgrade pip --quiet
& $pyExe -m pip install -r requirements.txt pyinstaller==6.16.0
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed." }

if ($UseExistingVerifiedEngine) {
    Write-Step "Verifying the supplied managed engine"
    & $pyExe scripts/openclank_engine.py verify --json
} else {
    Write-Step "Building and ACP-verifying the exact managed engine"
    & $pyExe scripts/openclank_engine.py build --json
}
if ($LASTEXITCODE -ne 0) { Fail "Managed engine build or verification failed." }

# Stage only current.json and the one activated versioned artifact. Copying the
# ambient libexec tree could silently ship stale engines from earlier builds.
$sourceEngineRoot = Join-Path $PSScriptRoot "libexec\openclank\engine"
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

Write-Step "Building portable exe bundle"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

$dataArgs = @(
    "--add-data", "static;static",
    "--add-data", "scripts;scripts",
    "--add-data", "mcp_servers;mcp_servers",
    "--add-data", "services/hwfit/data;services/hwfit/data",
    "--add-data", "config;config",
    "--add-data", "contracts;contracts",
    "--add-data", ((Join-Path $stageRoot "libexec") + ";libexec")
)

$pyInstallerExit = 1
try {
    & $pyExe -m PyInstaller --noconfirm --clean --onedir --console --noupx --contents-directory _internal --icon=static/icon.ico --name openclank --hidden-import=app --collect-submodules=keyring.backends @dataArgs openclank_entry.py
    $pyInstallerExit = $LASTEXITCODE
} finally {
    Remove-Item -Recurse -Force $stageRoot -ErrorAction SilentlyContinue
}
if ($pyInstallerExit -ne 0) { Fail "PyInstaller build failed." }

$bundleRoot = Join-Path $PSScriptRoot "dist\openclank"
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
if ([string]$engineProvenance.target -ne "windows-x64") { Fail "Packaged engine target is not Windows x64." }
$engineBinaryPath = Join-Path (Split-Path $engineProvenancePath -Parent) ([string]$engineProvenance.binary.name)
if (-not (Test-Path $engineBinaryPath -PathType Leaf)) { Fail "Packaged engine binary is missing." }

$relativeEngineProvenance = $engineProvenancePath.Substring($bundleRoot.Length + 1).Replace("\", "/")
$relativeEngineBinary = $engineBinaryPath.Substring($bundleRoot.Length + 1).Replace("\", "/")
$appVersion = (& $pyExe -c "from src.constants import APP_VERSION; print(APP_VERSION)").Trim()
$portableProvenance = [ordered]@{
    schema_version = 1
    product = "Open Clank"
    artifact_kind = "windows-x64-portable"
    version = $appVersion
    target = "windows-x64"
    public_entrypoint = "openclank.exe"
    contents_directory = "_internal"
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
