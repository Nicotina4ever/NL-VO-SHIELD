#requires -Version 5.1
<#
  DEV-ONLY runtime provisioning for NL VO Shield (Windows).
  Downloads python-build-standalone CPython into .local/runtime/windows-x64
  and installs the Python engine dependencies there. Never touches system Python,
  never writes outside the repository .local tree.

  Idempotent: re-running skips verified downloads and already-satisfied packages.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$PbsTag = '20261026',
  [string]$PbsFile = 'cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz',
  [switch]$SkipPackages
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$runtimeDir = Join-Path $RepoRoot '.local\runtime\windows-x64'
$dlDir = Join-Path $RepoRoot '.local\downloads'
$pythonExe = Join-Path $runtimeDir 'python.exe'
$marker = Join-Path $runtimeDir '.pbs-version'
$pbsUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$PbsTag/$([uri]::EscapeDataString($PbsFile))"

New-Item -ItemType Directory -Force $dlDir | Out-Null

function Download-IfMissing([string]$Url, [string]$Dest) {
  if (Test-Path $Dest) { Write-Host "[skip] present: $Dest"; return }
  Write-Host "[get ] $Url"
  $tmp = "$Dest.part"
  Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing -TimeoutSec 600
  Move-Item -Force $tmp $Dest
}

if (-not (Test-Path $pythonExe)) {
  $archive = Join-Path $dlDir $PbsFile
  Download-IfMissing $pbsUrl $archive
  Write-Host "[ext] $archive -> $runtimeDir"
  New-Item -ItemType Directory -Force $runtimeDir | Out-Null
  tar -xzf $archive -C $runtimeDir --strip-components=1
  if (-not (Test-Path $pythonExe)) { throw "Portable Python extraction failed: $pythonExe missing" }
  Set-Content -NoNewline -Path $marker -Value "$PbsTag`n$PbsFile"
} else {
  Write-Host "[skip] portable python present: $pythonExe"
}

$version = & $pythonExe -c "import sys;print(sys.version.split()[0])"
Write-Host "[ok  ] portable CPython $version at $pythonExe"

if (-not $SkipPackages) {
  & $pythonExe -m pip --version *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[pip ] bootstrapping pip"
    & $pythonExe -m ensurepip --upgrade
  }
  & $pythonExe -m pip install --disable-pip-version-check --no-warn-script-location --upgrade pip
  Write-Host "[pip ] installing engine dependencies (torch CPU may take a few minutes)"
  $req = Join-Path $RepoRoot 'packages\engine\requirements.txt'
  if (Test-Path $req) {
    & $pythonExe -m pip install --disable-pip-version-check --no-warn-script-location -r $req
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
  } else {
    & $pythonExe -m pip install --disable-pip-version-check --no-warn-script-location numpy scipy soundfile librosa
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
  }
  & $pythonExe -c "import numpy, scipy, soundfile, librosa; print('[ok  ] numpy',numpy.__version__,'| scipy',scipy.__version__,'| soundfile',soundfile.__version__,'| librosa',librosa.__version__)"
}

Write-Host "DONE: portable runtime ready under $runtimeDir"
