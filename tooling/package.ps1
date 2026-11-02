#requires -Version 5.1
<#
.SYNOPSIS
  NL VO Shield - package the Windows desktop installer(s) into dist/.

.DESCRIPTION
  Pipeline: leak guard -> frontend build -> engine sidecar -> `tauri build` bundles
  (NSIS + MSI on Windows, dmg/app on macOS, deb/AppImage on Linux) -> dist/<version>/
  with deterministic artifact names, SHA256SUMS, BUILD-INFO.json and a content scan.

  Signing:
    - If TAURI_SIGNING_PRIVATE_KEY is set AND the Tauri config has an updater pubkey,
      updater artifacts are signed by the Tauri bundler.
    - Otherwise artifacts are UNSIGNED and auto-update stays disabled; this is recorded
      in BUILD-INFO.json (never claim a signature that does not exist).

  Model policy (mission constraint): the base installer ships NO models and the app is
  portable-by-default: all runtime data lives inside `<install dir>/data` (models, cache,
  receipts, logs), never in %APPDATA%. If the install folder is not writable the app
  asks the user for a folder (no silent fallback).

.PARAMETER RepoRoot
  Optional repository root (default: discovered by walking up).

.PARAMETER Bundles
  Comma separated Tauri bundle targets. Windows default: nsis,msi.

.PARAMETER ExpectedVersion
  Refuse to package a different app version. Default: 1.0.0.

.PARAMETER SkipLeakGuard
  Do not run the repository leak guard (NOT recommended; recorded in BUILD-INFO.json).

.PARAMETER SkipSidecar
  Reuse the existing apps/desktop/src-tauri/engine payload (no rebuild).

.PARAMETER SkipInstall
  Skip `npm ci` even when apps/desktop/node_modules is missing (assumes deps present).

.PARAMETER WithAudioSeal
  Build the optional AudioSeal/torch engine package instead of the base package.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tooling\package.ps1
  powershell -ExecutionPolicy Bypass -File tooling\package.ps1 -Bundles nsis

.OUTPUTS
  Exit codes: 0 ok; 1 packaging failure; 2 prerequisite missing; 3 leak guard failed.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot,
  [string]$Bundles = '',
  [string]$ExpectedVersion = '1.0.0',
  [switch]$SkipLeakGuard,
  [switch]$SkipSidecar,
  [switch]$SkipInstall,
  [switch]$WithAudioSeal
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$script:LogFile = $null

function Write-Log {
  param([ValidateSet('INFO', 'OK', 'WARN', 'ERROR', 'STEP')][string]$Level, [string]$Message)
  $line = "[{0}] [{1}] {2}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Level, $Message
  $color = @{ INFO = 'Gray'; OK = 'Green'; WARN = 'Yellow'; ERROR = 'Red'; STEP = 'Cyan' }[$Level]
  Write-Host $line -ForegroundColor $color
  if ($script:LogFile) { try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 } catch { } }
}
function Fail { param([string]$Message, [int]$Code = 1) Write-Log ERROR $Message; exit $Code }
function Format-Size { param([long]$Bytes) if ($Bytes -ge 1GB) { '{0:N2} GB' -f ($Bytes / 1GB) } elseif ($Bytes -ge 1MB) { '{0:N1} MB' -f ($Bytes / 1MB) } elseif ($Bytes -ge 1KB) { '{0:N0} KB' -f ($Bytes / 1KB) } else { "$Bytes B" } }

function Find-RepoRoot {
  param([string]$Hint)
  $starts = @()
  if ($Hint) { $starts += $Hint }
  if ($PSScriptRoot) { $starts += $PSScriptRoot }
  $starts += (Get-Location).Path
  foreach ($start in $starts) {
    if ([string]::IsNullOrWhiteSpace($start)) { continue }
    try { $dir = (Resolve-Path -LiteralPath $start -ErrorAction Stop).Path } catch { continue }
    if (Test-Path -LiteralPath $dir -PathType Leaf) { $dir = Split-Path -Parent $dir }
    while ($dir) {
      if ((Test-Path -LiteralPath (Join-Path $dir 'assets\LogoMio.svg') -PathType Leaf) -and
          (Test-Path -LiteralPath (Join-Path $dir 'apps\desktop') -PathType Container)) { return $dir }
      $parent = Split-Path -Parent $dir
      if (-not $parent -or $parent -eq $dir) { break }
      $dir = $parent
    }
  }
  throw 'Repository root not found. Pass -RepoRoot <path>.'
}

try { $root = Find-RepoRoot -Hint $RepoRoot } catch { Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red; exit 2 }
$desktop = Join-Path $root 'apps\desktop'
$tauriDir = Join-Path $desktop 'src-tauri'
$confPath = Join-Path $tauriDir 'tauri.conf.json'
$logsDir = Join-Path $root '.local\logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$script:LogFile = Join-Path $logsDir ("package-{0}.log" -f (Get-Date).ToString('yyyyMMdd-HHmmss'))
New-Item -ItemType File -Force -Path $script:LogFile | Out-Null

Write-Log INFO 'NL VO Shield - package'
Write-Log INFO "repo root: $root"

if (-not (Test-Path -LiteralPath $confPath -PathType Leaf)) { Fail "Tauri config missing: $confPath (apps/desktop shell not ready)" 2 }
$conf = Get-Content -LiteralPath $confPath -Raw -Encoding UTF8 | ConvertFrom-Json
$product = [string]$conf.productName
if ([string]::IsNullOrWhiteSpace($product)) { $product = 'NL VO Shield' }
$version = [string]$conf.version
if ([string]::IsNullOrWhiteSpace($version)) {
  $pkg = Get-Content -LiteralPath (Join-Path $desktop 'package.json') -Raw | ConvertFrom-Json
  $version = [string]$pkg.version
}
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } elseif ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
Write-Log INFO "product: $product | version: $version | windows-$arch"
if (-not [string]::IsNullOrWhiteSpace($ExpectedVersion) -and $version -ne $ExpectedVersion) {
  Fail "app version mismatch: tauri.conf.json reports '$version' but the release version must be '$ExpectedVersion'. Update apps/desktop version fields (tauri.conf.json, package.json, Cargo.toml) or pass -ExpectedVersion." 2
}

if ([string]::IsNullOrWhiteSpace($Bundles)) { $Bundles = 'nsis,msi' }
$bundleList = @($Bundles.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
Write-Log INFO "bundle targets: $($bundleList -join ', ')"

# ------------------------------------------------------------- prerequisite
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail 'Node.js is required to build the frontend' 2 }
$cli = Join-Path $desktop 'node_modules\@tauri-apps\cli\tauri.js'
$npmLock = Join-Path $desktop 'package-lock.json'
$pnpmLock = Join-Path $desktop 'pnpm-lock.yaml'

# ---------------------------------------------------------------- leak guard
if (-not $SkipLeakGuard) {
  Write-Log STEP 'step 1/7 - leak guard (repository)'
  & node (Join-Path $root 'tooling\leak-guard.mjs')
  if ($LASTEXITCODE -ne 0) { Fail 'leak guard found violations - packaging aborted (fix or move those files)' 3 }
  Write-Log OK 'leak guard: clean'
} else {
  Write-Log WARN 'step 1/7 - leak guard SKIPPED by request (recorded in BUILD-INFO.json)'
}

# ------------------------------------------------------------- frontend build
Write-Log STEP 'step 2/7 - frontend dependencies and build'
Push-Location $desktop
try {
  $nodeModules = Join-Path $desktop 'node_modules'
  if (-not (Test-Path -LiteralPath $nodeModules)) {
    if ($SkipInstall) { Fail 'apps/desktop/node_modules missing and -SkipInstall was specified' 2 }
    if (Test-Path -LiteralPath $pnpmLock) {
      Write-Log INFO 'pnpm install --frozen-lockfile'
      & pnpm install --frozen-lockfile
    } elseif (Test-Path -LiteralPath $npmLock) {
      Write-Log INFO 'npm ci'
      & npm ci
    } else {
      Write-Log INFO 'npm install (no lockfile)'
      & npm install
    }
    if ($LASTEXITCODE -ne 0) { Fail 'dependency installation failed' 1 }
  } else {
    Write-Log INFO 'node_modules already present - skipping dependency installation'
  }
  Write-Log INFO 'npm run build'
  & npm run build
  if ($LASTEXITCODE -ne 0) { Fail 'frontend build failed' 1 }
  Write-Log OK 'frontend build complete'
} finally { Pop-Location }
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) { Fail "Tauri CLI missing after install: $cli" 2 }

# ------------------------------------------------------------------- sidecar
if (-not $SkipSidecar) {
  Write-Log STEP 'step 3/7 - engine sidecar'
  $sidecar = Join-Path $root 'tooling\build-engine-sidecar.ps1'
  $splat = @{}
  if ($WithAudioSeal) { $splat['WithAudioSeal'] = $true }
  & powershell -NoProfile -ExecutionPolicy Bypass -File $sidecar @splat
  if ($LASTEXITCODE -ne 0) { Fail "engine sidecar build failed (exit $LASTEXITCODE)" 1 }
  Write-Log OK 'engine sidecar ready'
} else {
  Write-Log WARN 'step 3/7 - sidecar build skipped by request'
}
$engineDir = Join-Path $tauriDir 'engine'
if (-not (Test-Path -LiteralPath $engineDir -PathType Container)) { Fail "engine payload missing: $engineDir (run tooling\build-engine-sidecar.ps1)" 2 }

# --------------------------------------- sidecar provenance + data policy
Write-Log STEP 'step 3b/7 - sidecar provenance and portable-data policy checks'
$engineManifestPath = Join-Path $engineDir 'build-manifest.json'
if (-not (Test-Path -LiteralPath $engineManifestPath -PathType Leaf)) {
  Fail "engine build manifest missing: $engineManifestPath (rebuild the sidecar)" 1
}
$engineManifest = Get-Content -LiteralPath $engineManifestPath -Raw | ConvertFrom-Json
$runtimePy = Join-Path $engineDir 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $runtimePy -PathType Leaf)) {
  Fail "engine payload is not a Windows runtime: runtime\python.exe missing (rebuild on Windows with the project Python)" 1
}
$frozenExes = @(Get-ChildItem -LiteralPath (Join-Path $engineDir 'frozen') -Recurse -File -Filter '*.exe' -ErrorAction SilentlyContinue)
if ($frozenExes.Count -eq 0) { Fail 'engine payload has no Windows frozen executable (frozen\nlvo-engine\nlvo-engine.exe missing)' 1 }
$foreignBins = @(Get-ChildItem -LiteralPath $engineDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Extension -in @('.so', '.dylib') -or $_.Name -like 'libpython*.so*'
  })
if ($foreignBins.Count -gt 0) {
  Fail ("engine payload contains foreign/Unix binaries ({0} files, e.g. {1}); rebuild the native Windows sidecar" -f $foreignBins.Count, $foreignBins[0].Name) 1
}
$pythonPath = [string]$engineManifest.python.path
$enginePlatform = [string]$engineManifest.platform
if ($enginePlatform -and $enginePlatform -notlike 'windows*') {
  Fail "engine payload platform '$enginePlatform' does not match Windows (rebuild the native sidecar)" 1
}
if ($pythonPath -match '^/mnt/' -or $pythonPath -match '^/home/' -or $pythonPath -match '^/Users/') {
  Fail "engine manifest provenance is not a Windows developer path: $pythonPath (rebuild on Windows)" 1
}
if ($engineManifest.python.source -eq 'portable-runtime' -and $enginePlatform -ne 'windows-x64' -and $enginePlatform -ne 'windows-arm64') {
  Fail 'engine payload provenance is ambiguous: rebuild the native Windows sidecar with the project Python' 1
}
$modelExtensions = @('.pth', '.pt', '.onnx', '.safetensors', '.gguf', '.ckpt', '.wav', '.mp3', '.flac', '.m4a')
# Library test fixtures under site-packages are not shipped models: only check the app payload.
$modelFiles = @(Get-ChildItem -LiteralPath $engineDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $modelExtensions -contains $_.Extension.ToLowerInvariant() -and $_.FullName -notmatch '[\\/]site-packages[\\/]'
  })
if ($modelFiles.Count -gt 0) {
  Fail ("engine payload contains model/audio payloads ({0} files, e.g. {1}); the base package must ship no models" -f $modelFiles.Count, $modelFiles[0].Name) 1
}
if ($WithAudioSeal -and -not [bool]$engineManifest.with_audio_seal) {
  Fail 'WithAudioSeal requested but the existing engine payload was built without AudioSeal' 1
}
$c2paPayload = Join-Path $engineDir 'bin\c2patool\c2patool.exe'
$c2paManifest = $engineManifest.c2patool
if (Test-Path -LiteralPath $c2paPayload -PathType Leaf) {
  if (-not (Test-Path -LiteralPath (Join-Path $engineDir 'THIRD-PARTY-NOTICES.txt') -PathType Leaf)) {
    Fail 'engine payload bundles c2patool but THIRD-PARTY-NOTICES.txt is missing' 1
  }
  Write-Log OK ("c2patool bundled: version {0}, sha256 {1}" -f $c2paManifest.version, ([string]$c2paManifest.sha256).Substring(0, 12))
} elseif ($c2paManifest -and $c2paManifest.bundled) {
  Fail 'engine manifest says c2patool is bundled but bin\c2patool\c2patool.exe is missing' 1
} else {
  Write-Log WARN 'c2patool is NOT bundled: the C2PA engine will be unavailable in this installer (honest limitation).'
}
Write-Log OK ("engine payload: Windows runtime + {0} frozen executable(s), {1} MB, no models/audio, provenance {2} {3}" -f `
    $frozenExes.Count, [math]::Round(((Get-ChildItem -LiteralPath $engineDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1), `
    $engineManifest.python.source, $engineManifest.python.version)

$pathsRs = Join-Path $tauriDir 'src\paths.rs'
if (-not (Test-Path -LiteralPath $pathsRs -PathType Leaf)) { Fail "paths.rs missing: $pathsRs" 1 }
$pathsText = Get-Content -LiteralPath $pathsRs -Raw
if ($pathsText -notmatch 'install_dir\.join\("data"\)') {
  Fail 'portable-data policy not implemented: paths.rs must default to <exe dir>/data (install_dir.join("data"))' 1
}
if ($pathsText -match 'app_data_dir\(') {
  Fail 'portable-data policy violated: paths.rs still uses app_data_dir() (default data must live inside the installation folder)' 1
}
if ($pathsText -match 'LOCALAPPDATA') {
  Write-Log WARN 'paths.rs mentions LOCALAPPDATA: allowed only for the minimal settings pointer when the install folder is not writable - must be documented and must never store models/data there.'
}
Write-Log OK 'portable-data policy: default data dir is <install dir>/data (no AppData default)'

# --------------------------------------------------------------- tauri build
Write-Log STEP 'step 4/7 - tauri build bundle'
$signed = -not [string]::IsNullOrWhiteSpace($env:TAURI_SIGNING_PRIVATE_KEY)
if ($signed) { Write-Log INFO 'TAURI_SIGNING_PRIVATE_KEY present: updater artifacts will be signed by the bundler' }
else { Write-Log WARN 'TAURI_SIGNING_PRIVATE_KEY absent: artifacts will be UNSIGNED and auto-update stays disabled' }

$targetDir = Join-Path $tauriDir 'target\release\bundle'
if (Test-Path -LiteralPath $targetDir) { Remove-Item -LiteralPath $targetDir -Recurse -Force -ErrorAction SilentlyContinue }

Push-Location $desktop
try {
  $tauriArgs = @($cli, 'build', '--bundles', ($bundleList -join ','))
  Write-Log INFO ("node {0}" -f ($tauriArgs -join ' '))
  & node @tauriArgs
  $tauriExit = $LASTEXITCODE
  if ($tauriExit -ne 0) {
    Write-Log WARN "tauri build failed (exit $tauriExit); retrying with updater artifacts disabled"
    $override = Join-Path $root '.local\cache\tauri-noupdater.json'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $override) | Out-Null
    '{ "bundle": { "createUpdaterArtifacts": false } }' | Set-Content -LiteralPath $override -Encoding ASCII
    & node $cli build --bundles ($bundleList -join ',') --config $override
    if ($LASTEXITCODE -ne 0) { Fail "tauri build failed (exit $LASTEXITCODE); see the log above" 1 }
  }
} finally { Pop-Location }

if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) { Fail "tauri build produced no bundle directory: $targetDir" 1 }

# -------------------------------------------------------------- collect dist
Write-Log STEP 'step 5/7 - collecting artifacts into dist'
$distRoot = Join-Path $root 'dist'
$dist = Join-Path $distRoot $version
if (Test-Path -LiteralPath $dist) { Remove-Item -LiteralPath $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist | Out-Null

function Get-StableName {
  param([string]$Kind, [string]$Extension, [string]$OriginalBase)
  $base = $OriginalBase
  if ($Kind -eq 'nsis') { $base = "${product}_${version}_windows-$arch-setup" }
  elseif ($Kind -eq 'msi') { $base = "${product}_${version}_windows-$arch" }
  elseif ($Kind -eq 'dmg') { $base = "${product}_${version}_macos-$arch" }
  elseif ($Kind -eq 'app') { $base = "${product}_${version}_macos-$arch.app" }
  elseif ($Kind -eq 'deb') { $base = "${product}_${version}_linux-$arch" }
  elseif ($Kind -eq 'appimage') { $base = "${product}_${version}_linux-$arch" }
  elseif ($Kind -eq 'rpm') { $base = "${product}_${version}_linux-$arch" }
  else { $base = "${product}_${version}_${Kind}" }
  $base = $base -replace '[^\w\.\-]+', '-' -replace '-{2,}', '-'
  if ($Extension) { return "$base$Extension" }
  return $base
}

$artifacts = New-Object System.Collections.Generic.List[object]
$bundleDirs = @{
  nsis = @{ Dir = 'nsis'; Ext = '.exe' }
  msi = @{ Dir = 'msi'; Ext = '.msi' }
  dmg = @{ Dir = 'dmg'; Ext = '.dmg' }
  deb = @{ Dir = 'deb'; Ext = '.deb' }
  appimage = @{ Dir = 'appimage'; Ext = '.AppImage' }
  rpm = @{ Dir = 'rpm'; Ext = '.rpm' }
}
foreach ($kind in $bundleList) {
  $info = $bundleDirs[$kind.ToLowerInvariant()]
  if (-not $info) { continue }
  $dir = Join-Path $targetDir $info.Dir
  if (-not (Test-Path -LiteralPath $dir -PathType Container)) { Write-Log WARN "bundle directory missing: $dir"; continue }
  $files = @(Get-ChildItem -LiteralPath $dir -File -Filter ("*" + $info.Ext) -ErrorAction SilentlyContinue)
  foreach ($file in $files) {
    $destName = Get-StableName -Kind $kind.ToLowerInvariant() -Extension $info.Ext -OriginalBase $file.BaseName
    $dest = Join-Path $dist $destName
    Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
    $sig = "$($file.FullName).sig"
    if (Test-Path -LiteralPath $sig) { Copy-Item -LiteralPath $sig -Destination "$dest.sig" -Force }
    $artifacts.Add([pscustomobject]@{ File = $destName; Bytes = (Get-Item -LiteralPath $dest).Length; Kind = $kind })
    Write-Log OK ("artifact: {0} ({1})" -f $destName, (Format-Size (Get-Item -LiteralPath $dest).Length))
  }
  if ($kind -eq 'app') {
    $apps = @(Get-ChildItem -LiteralPath $dir -Directory -Filter '*.app' -ErrorAction SilentlyContinue)
    foreach ($appDir in $apps) {
      $archive = Get-StableName -Kind 'app' -Extension '.tar.gz' -OriginalBase $appDir.BaseName
      $dest = Join-Path $dist $archive
      & tar -czf $dest -C $appDir.Parent.FullName $appDir.Name
      if ($LASTEXITCODE -ne 0) { Fail "failed to archive .app bundle: $($appDir.FullName)" 1 }
      $sig = "$($appDir.FullName).tar.gz.sig"
      if (Test-Path -LiteralPath $sig) { Copy-Item -LiteralPath $sig -Destination "$dest.sig" -Force }
      $artifacts.Add([pscustomobject]@{ File = $archive; Bytes = (Get-Item -LiteralPath $dest).Length; Kind = 'app' })
      Write-Log OK ("artifact: {0} ({1})" -f $archive, (Format-Size (Get-Item -LiteralPath $dest).Length))
    }
  }
}
if ($artifacts.Count -eq 0) { Fail 'no installer artifact was produced by tauri build' 1 }

# -------------------------------------------------------- checksums + info
Write-Log STEP 'step 6/7 - checksums, manifest and artifact scan'
$sumLines = @()
foreach ($a in $artifacts) {
  $full = Join-Path $dist $a.File
  $hash = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant()
  $sumLines += "$hash  $($a.File)"
}
$sumLines | Set-Content -LiteralPath (Join-Path $dist 'SHA256SUMS') -Encoding ASCII

$publisher = 'Nicola Lanci'
$copyright = '© 2026 Nicola Lanci — NL VO Shield'
$homepage = 'https://www.nicolalanci.com'
$contact = 'info@nicolalanci.com'
$repoUrl = 'https://github.com/Nicotina4ever/NL-VO-SHIELD'
$confPublisher = [string]$conf.bundle.publisher
$confCopyright = [string]$conf.bundle.copyright
$confHomepage = [string]$conf.bundle.homepage
if ($confPublisher -and $confPublisher -ne $publisher) { Write-Log WARN "tauri.conf publisher '$confPublisher' differs from the required '$publisher'" }
if ($confCopyright -and $confCopyright -ne $copyright) { Write-Log WARN "tauri.conf copyright '$confCopyright' differs from the required '$copyright'" }
if ($confHomepage -and $confHomepage -ne $homepage) { Write-Log WARN "tauri.conf homepage '$confHomepage' differs from the required '$homepage'" }

$buildInfo = [ordered]@{
  schema = 1
  product = $product
  version = $version
  os = 'windows'
  arch = $arch
  generated_at = (Get-Date).ToString('o')
  publisher = $publisher
  copyright = $copyright
  homepage = $homepage
  contact = $contact
  repository = $repoUrl
  signed = $signed
  auto_update = $(if ($signed) { 'enabled-if-updater-pubkey-configured' } else { 'disabled-no-signing-key' })
  leak_guard = $(if ($SkipLeakGuard) { 'skipped' } else { 'passed' })
  bundle_targets = $bundleList
  with_audio_seal = [bool]$WithAudioSeal
  models_bundled = $false
  models_location = '<install dir>\data\models (portable by default; the app asks for a folder if the install dir is not writable; no %APPDATA% data)'
  data_dir_policy = 'install'
  sidecar = @{ platform = [string]$engineManifest.platform; python = $engineManifest.python; runtime = [bool]$engineManifest.runtime; pyinstaller = [bool]$engineManifest.pyinstaller; c2patool = $engineManifest.c2patool }
  artifacts = @($artifacts | ForEach-Object { @{ file = $_.File; bytes = $_.Bytes; sha256 = (Get-FileHash -LiteralPath (Join-Path $dist $_.File) -Algorithm SHA256).Hash.ToLowerInvariant() } })
  artifacts_bytes = ($artifacts | Measure-Object -Property Bytes -Sum).Sum
}
$buildInfo | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $dist 'BUILD-INFO.json') -Encoding UTF8

& node (Join-Path $root 'tooling\leak-guard.mjs') --scan $dist
if ($LASTEXITCODE -ne 0) { Fail 'artifact content scan failed: private paths/lab/runtime traces inside the bundle' 3 }
Write-Log OK 'artifact content scan: clean (no lab/private paths, no model/audio/key payloads)'

Write-Log STEP 'step 7/7 - summary'
$totalBytes = ($artifacts | Measure-Object -Property Bytes -Sum).Sum
Write-Log OK ("dist: {0} ({1} in {2} artifacts)" -f $dist, (Format-Size $totalBytes), $artifacts.Count)
foreach ($a in $artifacts) { Write-Log INFO ("  {0} - {1}" -f $a.File, (Format-Size $a.Bytes)) }
Write-Log INFO "SHA256SUMS: $(Join-Path $dist 'SHA256SUMS')"
Write-Log INFO "BUILD-INFO.json: $(Join-Path $dist 'BUILD-INFO.json')"
Write-Log INFO "log: $($script:LogFile)"
if (-not $signed) { Write-Log WARN 'UNSIGNED build: macOS/Windows trust warnings are expected; auto-update is disabled until a signing key is configured.' }
exit 0
