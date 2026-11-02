#requires -Version 5.1
<#
.SYNOPSIS
  NL VO Shield - development bootstrap for Windows (idempotent).

.DESCRIPTION
  Prepares a local, isolated development runtime *inside the repository* without
  touching system Python, global pip, PATH, or the registry.

  What it does (idempotent, safe to run twice):
    1. discovers the repository root by walking up from this script (no hardcoded paths);
    2. resolves the project Python:
         a. sibling ..\python_win\python.exe  (the project interpreter, preferred);
         b. .local\runtime\windows-<arch>\python.exe (python-build-standalone);
         c. only with -AllowDownload: explicit DEV-ONLY download of a pinned
            python-build-standalone release, checksum verified before extraction;
    3. repairs pip with the project's bundled get-pip.py if `python -m pip` is broken;
    4. installs packages\engine\requirements.txt (and requirements-optional.txt with -WithOptional);
    5. verifies the modules and the nl_vo_shield package;
    6. creates .local\{runtime,cache,envs,downloads,logs,tmp} and writes a timestamped log;
    7. resumes after an interrupted run (step journal in .local\.bootstrap-state.json).

  Never uses `python`/`py` from PATH, never creates a venv, never edits the global
  environment: cache/temp variables are set only for this process and its children.

.PARAMETER RepoRoot
  Optional explicit repository root. Default: auto-discovered.

.PARAMETER Check
  Verification only: no installs, no downloads, no writes.
  Exit 0 only if Python, pip and all base modules are already usable.

.PARAMETER WithOptional
  Also install packages\engine\requirements-optional.txt (torch/AudioSeal are large).
  Default: skipped (the base product works without them).

.PARAMETER AllowDownload
  DEV ONLY: allow downloading portable CPython if ..\python_win is absent.
  The download is logged loudly and verified against a pinned SHA-256.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1
  powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1 -Check
  powershell -ExecutionPolicy Bypass -File tooling\bootstrap.ps1 -AllowDownload

.OUTPUTS
  Exit codes: 0 = ready; 1 = check/dependency failure; 2 = Python not found;
  3 = pip could not be repaired; 4 = package installation failed.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot,
  [switch]$Check,
  [switch]$WithOptional,
  [switch]$AllowDownload
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$script:IsCheck = [bool]$Check
$script:LogFile = $null

# Pinned python-build-standalone release (DEV fallback only, always checksum-verified).
$script:PbsTag = '20261026'
$script:PbsVersion = '3.12.16'
$script:PbsAssets = @{
  'windows-x64'   = @{ File = 'cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz'; Sha256 = '1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149' }
  'windows-arm64' = @{ File = 'cpython-3.12.16+20261026-aarch64-pc-windows-msvc-install_only_stripped.tar.gz'; Sha256 = '3b2e4da9209cec3e0eaf4db31176977a88df32d598ff94a7899c34cd8304db04' }
}
$script:PbsBaseUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$($script:PbsTag)"

# Base engine modules that must import for development (package -> import module).
$script:BaseModules = @(
  @{ Package = 'numpy'; Module = 'numpy' },
  @{ Package = 'scipy'; Module = 'scipy' },
  @{ Package = 'soundfile'; Module = 'soundfile' },
  @{ Package = 'librosa'; Module = 'librosa' },
  @{ Package = 'cryptography'; Module = 'cryptography' },
  @{ Package = 'pyloudnorm'; Module = 'pyloudnorm' }
)
$script:OptionalModules = @(
  @{ Package = 'torch'; Module = 'torch' },
  @{ Package = 'audioseal'; Module = 'audioseal' },
  @{ Package = 'PyInstaller'; Module = 'PyInstaller' }
)

# --------------------------------------------------------------------------- io
function Write-Log {
  param(
    [ValidateSet('INFO', 'OK', 'WARN', 'ERROR', 'STEP')][string]$Level,
    [string]$Message
  )
  $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  $line = "[$stamp] [$Level] $Message"
  $color = @{ INFO = 'Gray'; OK = 'Green'; WARN = 'Yellow'; ERROR = 'Red'; STEP = 'Cyan' }[$Level]
  Write-Host $line -ForegroundColor $color
  if ($script:LogFile) {
    try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 } catch { }
  }
}

function Fail-Bootstrap {
  param([string]$Message, [int]$Code = 1, [string]$Hint)
  Write-Log ERROR $Message
  if ($Hint) { Write-Log INFO "hint: $Hint" }
  if ($script:LogFile) { Write-Log INFO "log: $($script:LogFile)" }
  exit $Code
}

# ------------------------------------------------------------------ discovery
function Find-RepoRoot {
  param([string]$Hint)
  $starts = New-Object System.Collections.Generic.List[string]
  if ($Hint) { $starts.Add($Hint) }
  if ($PSScriptRoot) { $starts.Add($PSScriptRoot) }
  $starts.Add((Get-Location).Path)
  foreach ($start in $starts) {
    if ([string]::IsNullOrWhiteSpace($start)) { continue }
    try { $dir = (Resolve-Path -LiteralPath $start -ErrorAction Stop).Path } catch { continue }
    if (Test-Path -LiteralPath $dir -PathType Leaf) { $dir = Split-Path -Parent $dir }
    while ($dir) {
      if (Test-Path -LiteralPath (Join-Path $dir 'assets\LogoMio.svg') -PathType Leaf) { return $dir }
      $parent = Split-Path -Parent $dir
      if (-not $parent -or $parent -eq $dir) { break }
      $dir = $parent
    }
  }
  throw "Repository root not found: no ancestor of '$($PSScriptRoot)' contains assets\LogoMio.svg. Pass -RepoRoot <path>."
}

function Get-WindowsArch {
  if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { return 'arm64' }
  if ([Environment]::Is64BitOperatingSystem) { return 'x64' }
  return 'x86'
}

# --------------------------------------------------------------------- python
function Get-ProjectPython {
  param([string]$Root, [string]$Arch, [switch]$NoDownload)

  # a) sibling project interpreter: <parent>\python_win\python.exe
  $sibling = Join-Path (Split-Path -Parent $Root) 'python_win\python.exe'
  if (Test-Path -LiteralPath $sibling -PathType Leaf) {
    return @{ Path = $sibling; Kind = 'project-python' }
  }

  # b) portable runtime already installed under .local/runtime
  $portableDir = Join-Path $Root ".local\runtime\windows-$Arch"
  $portable = Join-Path $portableDir 'python.exe'
  if (Test-Path -LiteralPath $portable -PathType Leaf) {
    return @{ Path = $portable; Kind = 'portable-runtime' }
  }

  if ($NoDownload) { return $null }

  # c) explicit DEV-ONLY download (never silent)
  $asset = $script:PbsAssets["windows-$Arch"]
  if (-not $asset) {
    Write-Log WARN "No python-build-standalone asset pinned for windows-$Arch; place the project Python manually."
    return $null
  }
  Write-Log WARN 'DEV-ONLY: project Python missing; downloading portable CPython into .local\runtime.'
  Write-Log WARN "  source: $script:PbsBaseUrl/$($asset.File)"
  Write-Log WARN "  pinned SHA-256: $($asset.Sha256)"
  $downloads = Join-Path $Root '.local\downloads'
  New-Item -ItemType Directory -Force -Path $downloads | Out-Null
  $archive = Join-Path $downloads $asset.File
  if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $partial = "$archive.part"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    $uri = "$script:PbsBaseUrl/$([uri]::EscapeDataString($asset.File))"
    try {
      Invoke-WebRequest -Uri $uri -OutFile $partial -UseBasicParsing -TimeoutSec 900
    } catch {
      if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
      Fail-Bootstrap "download failed: $($_.Exception.Message)" 2 'check connectivity, or provide ..\python_win\python.exe'
    }
    Move-Item -LiteralPath $partial -Destination $archive -Force
  }
  $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($hash -ne $asset.Sha256.ToLowerInvariant()) {
    Remove-Item -LiteralPath $archive -Force
    Fail-Bootstrap "checksum mismatch for $($asset.File): got $hash expected $($asset.Sha256)" 2 'refusing to use an unverified runtime'
  }
  Write-Log OK "download verified: $($asset.File)"
  New-Item -ItemType Directory -Force -Path $portableDir | Out-Null
  & tar -xzf $archive -C $portableDir --strip-components=1
  if ($LASTEXITCODE -ne 0) { Fail-Bootstrap "archive extraction failed (tar exit $LASTEXITCODE)" 2 }
  if (-not (Test-Path -LiteralPath $portable -PathType Leaf)) { Fail-Bootstrap 'portable Python extraction produced no python.exe' 2 }
  Set-Content -LiteralPath (Join-Path $portableDir '.pbs-version') -Value "$($script:PbsVersion)`n$($asset.File)" -Encoding ASCII
  return @{ Path = $portable; Kind = 'portable-runtime' }
}

function Get-PythonDescription {
  param([string]$PythonExe)
  try {
    $run = & $PythonExe -c "import sys, platform; print(sys.version.split()[0]); print(platform.architecture()[0])"
    $lines = @($run)
    return @{ Version = [string]$lines[0]; Bitness = [string]$lines[1] }
  } catch {
    return @{ Version = 'unknown'; Bitness = 'unknown' }
  }
}

function Invoke-PipCheck {
  param([string]$PythonExe)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $PythonExe -m pip --version 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  return @{ Ok = ($code -eq 0); Output = (($out | Out-String).Trim()); Code = $code }
}

function Repair-Pip {
  param([string]$Root, [string]$PythonExe)
  $pythonDir = Split-Path -Parent $PythonExe
  $getPip = Join-Path $pythonDir 'get-pip.py'
  if (-not (Test-Path -LiteralPath $getPip -PathType Leaf)) {
    $siblingGetPip = Join-Path (Split-Path -Parent $Root) 'python_win\get-pip.py'
    if (Test-Path -LiteralPath $siblingGetPip -PathType Leaf) { $getPip = $siblingGetPip }
  }
  if (Test-Path -LiteralPath $getPip -PathType Leaf) {
    Write-Log STEP "repairing pip with bundled get-pip.py: $getPip"
    & $PythonExe $getPip --no-warn-script-location --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Fail-Bootstrap "get-pip.py failed (exit $LASTEXITCODE)" 3 }
  } else {
    Write-Log STEP 'repairing pip with ensurepip'
    & $PythonExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { Fail-Bootstrap 'pip is broken and no get-pip.py/ensurepip is available' 3 }
  }
  $pip = Invoke-PipCheck $PythonExe
  if (-not $pip.Ok) { Fail-Bootstrap "pip still not usable after repair: $($pip.Output)" 3 }
  Write-Log OK "pip repaired: $($pip.Output)"
}

# ------------------------------------------------------------------- helpers
function Get-FileSha256 {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-JsonSafe {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
  try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Save-State {
  param([hashtable]$State, [string]$Root)
  if ($script:IsCheck) { return }
  try {
    $State['updated_at'] = (Get-Date).ToString('o')
    ($State | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath (Join-Path $Root '.local\.bootstrap-state.json') -Encoding UTF8
  } catch {
    Write-Log WARN "could not write bootstrap state: $($_.Exception.Message)"
  }
}

function Test-Modules {
  param([string]$PythonExe, [array]$Modules)
  $names = ($Modules | ForEach-Object { $_.Module }) -join ','
  $code = "import importlib, sys`n" +
          "missing = []`n" +
          "for name in '$names'.split(','):`n" +
          "    if not name: continue`n" +
          "    try: importlib.import_module(name)`n" +
          "    except Exception as exc: missing.append(name + ' (' + type(exc).__name__ + ')' )`n" +
          "if missing:`n" +
          "    print('MISSING: ' + ', '.join(missing)); sys.exit(1)`n" +
          "print('ALL_OK')`n"
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $PythonExe -c $code 2>&1
    $exit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  return @{ Ok = ($exit -eq 0); Output = (($out | Out-String).Trim()) }
}

# ---------------------------------------------------------------------- main
try {
  $root = Find-RepoRoot -Hint $RepoRoot
} catch {
  Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
  exit 2
}

$arch = Get-WindowsArch

if (-not $script:IsCheck) {
  $logsDir = Join-Path $root '.local\logs'
  New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
  $script:LogFile = Join-Path $logsDir ("bootstrap-{0}.log" -f (Get-Date).ToString('yyyyMMdd-HHmmss'))
  New-Item -ItemType File -Force -Path $script:LogFile | Out-Null
}

Write-Log INFO "NL VO Shield bootstrap - $(if ($script:IsCheck) { 'CHECK ONLY' } else { 'install' })"
Write-Log INFO "repository root: $root"
Write-Log INFO "OS: $([Environment]::OSVersion.VersionString) | arch: $arch | PowerShell: $($PSVersionTable.PSVersion)"
Write-Log INFO "repo python rule: ..\python_win\python.exe (sibling), fallback .local\runtime\windows-$arch"

# ---------------- step 1: directories
$localDirs = @('runtime', 'cache', 'envs', 'downloads', 'logs', 'tmp')
Write-Log STEP 'step 1/6 - local directories (.local/{runtime,cache,envs,downloads,logs,tmp})'
$missingDirs = @()
foreach ($d in $localDirs) {
  $full = Join-Path $root ".local\$d"
  if (Test-Path -LiteralPath $full -PathType Container) { continue }
  $missingDirs += $full
  if (-not $script:IsCheck) { New-Item -ItemType Directory -Force -Path $full | Out-Null }
}
if ($missingDirs.Count -eq 0) { Write-Log OK 'all .local directories already present' }
elseif ($script:IsCheck) { Write-Log WARN "missing directories (would create): $($missingDirs -join ', ')" }
else { Write-Log OK "created: $($missingDirs -join ', ')" }

# ---------------- step 2: python
Write-Log STEP 'step 2/6 - resolving the project Python (never system python/PATH)'
$python = $null
$noDownload = $script:IsCheck -or (-not $AllowDownload)
try {
  $python = Get-ProjectPython -Root $root -Arch $arch -NoDownload:$noDownload
} catch {
  Fail-Bootstrap $_.Exception.Message 2
}
if (-not $python) {
  $expected = Join-Path (Split-Path -Parent $root) 'python_win\python.exe'
  Write-Log ERROR "no project Python found: expected $expected or .local\runtime\windows-$arch\python.exe"
  if ($script:IsCheck) { Write-Log INFO 'run without -Check and with -AllowDownload to provision the portable runtime (DEV only)' }
  exit 2
}
$pyExe = $python.Path
$pyInfo = Get-PythonDescription $pyExe
Write-Log OK "python ($($python.Kind)): $pyExe"
Write-Log INFO "version: $($pyInfo.Version) ($($pyInfo.Bitness))"
if ($pyInfo.Version -eq 'unknown') { Fail-Bootstrap "project Python does not run: $pyExe" 2 }
$versionParts = $pyInfo.Version.Split('.')
if ([int]$versionParts[0] -ne 3 -or [int]$versionParts[1] -lt 11) {
  Write-Log WARN "expected Python >= 3.11, found $($pyInfo.Version)"
}

# ---------------- step 3: pip
Write-Log STEP 'step 3/6 - pip availability'
$pip = Invoke-PipCheck $pyExe
if ($pip.Ok) {
  Write-Log OK "pip ready: $($pip.Output)"
} else {
  if ($script:IsCheck) {
    Write-Log ERROR "pip is broken: $($pip.Output)"
    exit 3
  }
  Write-Log WARN "pip broken: $($pip.Output)"
  Repair-Pip -Root $root -PythonExe $pyExe
}

# Isolated child-process environment (process-scoped only, never persisted globally).
$env:NLVS_PROJECT_ROOT = $root
$env:NLVS_ENGINE_PYTHON = $pyExe
$env:PIP_CACHE_DIR = Join-Path $root '.local\cache\pip'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:TORCH_HOME = Join-Path $root '.local\cache\torch'
$env:HF_HOME = Join-Path $root '.local\cache\huggingface'
$env:NUMBA_CACHE_DIR = Join-Path $root '.local\cache\numba'
if (-not $script:IsCheck) { New-Item -ItemType Directory -Force -Path (Join-Path $root '.local\tmp') | Out-Null }

# ---------------- step 4: requirements
Write-Log STEP 'step 4/6 - engine dependencies'
$enginePath = Join-Path $root 'packages\engine'
$requirements = Join-Path $enginePath 'requirements.txt'
$optionalRequirements = Join-Path $enginePath 'requirements-optional.txt'
$baseline = Join-Path $root 'tooling\requirements-baseline.txt'
$reqHash = Get-FileSha256 $requirements
$optHash = Get-FileSha256 $optionalRequirements
$state = @{
  schema = 1
  root = $root
  platform = "windows-$arch"
  status = 'running'
  started_at = (Get-Date).ToString('o')
  steps = @{}
  requirements_sha256 = $reqHash
  optional_sha256 = $optHash
  python = @{ path = $pyExe; kind = $python.Kind; version = $pyInfo.Version }
}

$previous = Read-JsonSafe (Join-Path $root '.local\.bootstrap-state.json')
$previousCompleted = ($null -ne $previous) -and ($previous.status -eq 'completed')
if ($null -ne $previous -and $previous.status -eq 'running') {
  Write-Log WARN 'previous bootstrap run was interrupted - resuming (all steps are idempotent)'
}

$probe = Test-Modules -PythonExe $pyExe -Modules $script:BaseModules
$fastPath = $previousCompleted -and `
            ($previous.requirements_sha256 -eq $reqHash) -and `
            ($previous.optional_sha256 -eq $optHash) -and `
            ($previous.python.path -eq $pyExe) -and `
            $probe.Ok

if ($script:IsCheck) {
  if ($probe.Ok) {
    Write-Log OK 'all base modules already importable'
  } else {
    Write-Log ERROR "modules missing or broken: $($probe.Output)"
  }
} elseif ($fastPath) {
  Write-Log OK 'dependencies up to date (requirements hashes unchanged, imports verified)'
} else {
  if (Test-Path -LiteralPath $requirements -PathType Leaf) {
    Write-Log STEP "pip install -r packages\engine\requirements.txt"
    & $pyExe -m pip install --disable-pip-version-check --no-warn-script-location -r $requirements
    if ($LASTEXITCODE -ne 0) { Fail-Bootstrap 'pip install of requirements.txt failed' 4 }
  } elseif (Test-Path -LiteralPath $baseline -PathType Leaf) {
    Write-Log WARN "packages\engine\requirements.txt missing (engine not ready) - installing tooling\requirements-baseline.txt"
    Write-Log STEP 'pip install -r tooling\requirements-baseline.txt'
    & $pyExe -m pip install --disable-pip-version-check --no-warn-script-location -r $baseline
    if ($LASTEXITCODE -ne 0) { Fail-Bootstrap 'pip install of baseline requirements failed' 4 }
  } else {
    Write-Log WARN 'no requirements.txt and no baseline file - dependency installation skipped'
  }

  if ($WithOptional -and (Test-Path -LiteralPath $optionalRequirements -PathType Leaf)) {
    Write-Log STEP 'pip install -r packages\engine\requirements-optional.txt (optional engines: torch/AudioSeal)'
    & $pyExe -m pip install --disable-pip-version-check --no-warn-script-location -r $optionalRequirements
    if ($LASTEXITCODE -ne 0) { Write-Log WARN "optional requirements failed (exit $LASTEXITCODE) - base product unaffected" }
  } elseif ($WithOptional) {
    Write-Log WARN 'requirements-optional.txt not present - optional engines skipped'
  }

  Write-Log OK 'engine dependencies installed'
  $state.steps['dependencies'] = @{ at = (Get-Date).ToString('o'); requirements_sha256 = $reqHash; optional_sha256 = $optHash }
  Save-State -State $state -Root $root
}

# ---------------- step 5: verification
Write-Log STEP 'step 5/6 - verifying imports'
$moduleCheck = Test-Modules -PythonExe $pyExe -Modules $script:BaseModules
if ($moduleCheck.Ok) {
  Write-Log OK 'base modules: numpy, scipy, soundfile, librosa, cryptography, pyloudnorm'
} else {
  Write-Log ERROR "base module check failed: $($moduleCheck.Output)"
  if (-not $script:IsCheck) { Fail-Bootstrap 'installed but modules do not import; inspect the log above' 4 }
}

$enginePkg = Join-Path $enginePath 'nl_vo_shield'
if (Test-Path -LiteralPath $enginePkg -PathType Container) {
  # The Windows project interpreter is an embedded CPython: its ._pth file ignores
  # PYTHONPATH, so insert the engine path explicitly in the probe.
  $probeScript = "import sys; sys.path.insert(0, sys.argv[1]); import nl_vo_shield; print('NLVS_ENGINE_IMPORT_OK', getattr(nl_vo_shield, '__version__', '?'))"
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $pyExe -c $probeScript $enginePath 2>&1
    $exit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($exit -eq 0) { Write-Log OK "engine package: $(($out | Out-String).Trim())" }
  else { Write-Log WARN "engine package not importable yet: $(($out | Out-String).Trim())" }
} else {
  Write-Log WARN 'packages\engine\nl_vo_shield not present yet (engine module not ready)'
}

if ($WithOptional -or $script:IsCheck) {
  $optCheck = Test-Modules -PythonExe $pyExe -Modules $script:OptionalModules
  if ($optCheck.Ok) { Write-Log OK 'optional modules: torch, audioseal, PyInstaller' }
  else { Write-Log INFO "optional modules not installed (OK for the base product): $($optCheck.Output)" }
}

# ---------------- step 6: summary
Write-Log STEP 'step 6/6 - summary'
Write-Log INFO "python: $pyExe"
Write-Log INFO "packages: $((Invoke-PipCheck $pyExe).Output)"
Write-Log INFO "engine path (for PYTHONPATH): $enginePath"

if ($script:IsCheck) {
  Write-Log INFO 'no files were written (-Check)'
  Write-Log OK 'CHECK PASS - development environment is ready'
  exit 0
}

if (-not $moduleCheck.Ok) { Fail-Bootstrap 'environment check failed' 4 }

$state.status = 'completed'
$state.finished_at = (Get-Date).ToString('o')
Save-State -State $state -Root $root
try {
  if ($script:LogFile) { Copy-Item -LiteralPath $script:LogFile -Destination (Join-Path $root '.local\logs\bootstrap-last.log') -Force }
} catch { }

Write-Log INFO "log: $($script:LogFile)"
Write-Log OK 'bootstrap complete - re-running this script is safe (idempotent)'
exit 0
