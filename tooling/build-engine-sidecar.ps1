#requires -Version 5.1
<#
.SYNOPSIS
  NL VO Shield - build the Windows engine sidecar payload.

.DESCRIPTION
  Produces apps/desktop/src-tauri/engine/ from the Python engine package:

    engine/
      runtime/                portable CPython + base engine dependencies
                              (launched as: runtime\python.exe -m nl_vo_shield.rpc)
      frozen/nlvo-engine/     PyInstaller one-folder frozen engine (optional entry point)
      build-manifest.json     what was built, from which interpreter, health results
      .nlvs-engine-build      marker: only directories carrying it are cleaned

  PyInstaller EXCLUDES torch / audioseal by default (base package). Use
  -WithAudioSeal to include the optional neural watermark stack (much larger).

  Safety: only apps/desktop/src-tauri/engine is ever cleaned, and only when it
  carries the .nlvs-engine-build marker (or -Force is given). No other file is removed.

.PARAMETER RepoRoot
  Optional repository root (default: discovered by walking up).

.PARAMETER WithAudioSeal
  Include torch + audioseal in the runtime, in the PyInstaller bundle and in the
  manifest. The resulting package is much larger (~2-3 GB) and is NOT the default release.

.PARAMETER SkipRuntime
  Do not produce engine/runtime (only the PyInstaller frozen app). Used by CI hosts
  where a relocatable interpreter is unavailable.

.PARAMETER SkipPyInstaller
  Do not produce the frozen app (only runtime + app package).

.PARAMETER Force
  Allow cleaning an engine directory that has no build marker.

.PARAMETER KeepWork
  Keep the PyInstaller work directory in .local/cache/pyinstaller.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tooling\build-engine-sidecar.ps1
  powershell -ExecutionPolicy Bypass -File tooling\build-engine-sidecar.ps1 -WithAudioSeal

.OUTPUTS
  Exit codes: 0 ok; 1 build failure; 2 prerequisite missing; 3 health check failed.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot,
  [switch]$WithAudioSeal,
  [switch]$SkipRuntime,
  [switch]$SkipPyInstaller,
  [switch]$Force,
  [switch]$KeepWork
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
          (Test-Path -LiteralPath (Join-Path $dir 'packages\engine') -PathType Container)) { return $dir }
      $parent = Split-Path -Parent $dir
      if (-not $parent -or $parent -eq $dir) { break }
      $dir = $parent
    }
  }
  throw 'Repository root not found. Pass -RepoRoot <path>.'
}

function Get-DirSize {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) { return 0 }
  $sum = 0
  Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object { $sum += $_.Length }
  return $sum
}

function Format-Size {
  param([long]$Bytes)
  if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
  if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
  if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
  return "$Bytes B"
}

function Test-PythonModule {
  param([string]$PythonExe, [string]$Module)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    & $PythonExe -c "import $Module" 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
  } finally { $ErrorActionPreference = $prev }
}

# ---------------------------------------------------------------- discovery
try { $root = Find-RepoRoot -Hint $RepoRoot } catch { Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red; exit 2 }

$engineSrc = Join-Path $root 'packages\engine'
$enginePkg = Join-Path $engineSrc 'nl_vo_shield'
$tauriDir = Join-Path $root 'apps\desktop\src-tauri'
$engineDir = Join-Path $tauriDir 'engine'
$marker = Join-Path $engineDir '.nlvs-engine-build'
$cacheDir = Join-Path $root '.local\cache\pyinstaller'
$logsDir = Join-Path $root '.local\logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$script:LogFile = Join-Path $logsDir ("engine-sidecar-{0}.log" -f (Get-Date).ToString('yyyyMMdd-HHmmss'))
New-Item -ItemType File -Force -Path $script:LogFile | Out-Null

Write-Log INFO 'NL VO Shield - engine sidecar build'
Write-Log INFO "repo root: $root"
Write-Log INFO "audio seal (optional torch stack): $([bool]$WithAudioSeal)"
Write-Log INFO "runtime: $(-not $SkipRuntime) | pyinstaller: $(-not $SkipPyInstaller)"

if (-not (Test-Path -LiteralPath $enginePkg -PathType Container)) { Fail "engine package missing: $enginePkg (engine module not ready)" 2 }
if (-not (Test-Path -LiteralPath $tauriDir -PathType Container)) { Fail "desktop shell missing: $tauriDir (apps/desktop is not ready)" 2 }

# ------------------------------------------------------------ python source
$winArch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } elseif ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
$pyExe = $null
$pyKind = ''
if ($env:NLVS_ENGINE_PYTHON -and (Test-Path -LiteralPath $env:NLVS_ENGINE_PYTHON -PathType Leaf)) {
  $pyExe = (Resolve-Path -LiteralPath $env:NLVS_ENGINE_PYTHON).Path; $pyKind = 'NLVS_ENGINE_PYTHON'
}
if (-not $pyExe) {
  $sibling = Join-Path (Split-Path -Parent $root) 'python_win\python.exe'
  if (Test-Path -LiteralPath $sibling -PathType Leaf) { $pyExe = (Resolve-Path -LiteralPath $sibling).Path; $pyKind = 'project-python' }
}
if (-not $pyExe) {
  $portable = Join-Path $root ".local\runtime\windows-$winArch\python.exe"
  if (Test-Path -LiteralPath $portable -PathType Leaf) { $pyExe = (Resolve-Path -LiteralPath $portable).Path; $pyKind = 'portable-runtime' }
}
if (-not $pyExe) { Fail 'no project Python found (expected ..\python_win\python.exe or .local\runtime\windows-<arch>)' 2 }
Write-Log OK "source python ($pyKind): $pyExe"

$pyVersion = (& $pyExe -c 'import sys; print(sys.version.split()[0])' 2>&1 | Out-String).Trim()
Write-Log INFO "python version: $pyVersion"

if (-not (Test-PythonModule $pyExe 'PyInstaller')) {
  Fail 'PyInstaller is not installed in the project Python. Run: & "<project python>" -m pip install pyinstaller' 2
}
foreach ($m in @('numpy', 'scipy', 'soundfile', 'librosa', 'cryptography', 'pyloudnorm')) {
  if (-not (Test-PythonModule $pyExe $m)) { Fail "missing base dependency '$m' in the project Python. Run tooling\bootstrap.ps1 first." 2 }
}
if ($WithAudioSeal) {
  foreach ($m in @('torch', 'audioseal')) {
    if (-not (Test-PythonModule $pyExe $m)) { Fail "-WithAudioSeal requested but '$m' is not installed in the project Python." 2 }
  }
}

# --------------------------------------------------------------- clean up
function Reset-EngineDir {
  param([string]$Dir, [string]$MarkerPath, [switch]$ForceClean)
  $leaf = Split-Path -Leaf $Dir
  $parentLeaf = Split-Path -Leaf (Split-Path -Parent $Dir)
  if ($leaf -ne 'engine' -or $parentLeaf -ne 'src-tauri') { throw "refusing to clean unexpected directory: $Dir" }
  if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null; return }
  $entries = @(Get-ChildItem -LiteralPath $Dir -Force | Where-Object { $_.Name -ne '.gitkeep' })
  if ($entries.Count -gt 0 -and -not (Test-Path -LiteralPath $MarkerPath) -and -not $ForceClean) {
    throw "refusing to clean '$Dir': no .nlvs-engine-build marker (use -Force only after checking the directory contents)"
  }
  foreach ($entry in $entries) { Remove-Item -LiteralPath $entry.FullName -Recurse -Force }
  Write-Log OK "cleaned generated engine directory (kept .gitkeep): $Dir"
}

Write-Log STEP 'step 1/6 - cleaning the generated engine directory'
try { Reset-EngineDir -Dir $engineDir -MarkerPath $marker -ForceClean:$Force } catch { Fail $_.Exception.Message 1 }
Set-Content -LiteralPath $marker -Value "NL VO Shield generated engine payload - safe to delete and rebuild.`nDo not commit." -Encoding ASCII

$baseReq = Join-Path $engineSrc 'requirements.txt'
$optionalReq = Join-Path $engineSrc 'requirements-optional.txt'
$siteRoot = $null

# ------------------------------------------------------------- runtime copy
if (-not $SkipRuntime) {
  Write-Log STEP 'step 2/6 - bundling portable Python runtime + app into engine/runtime'
  $runtimeDir = Join-Path $engineDir 'runtime'
  New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
  $srcDir = Split-Path -Parent $pyExe
  # Full CPython installs (python-build-standalone, setup-python, standard python.org)
  # keep the stdlib in Lib\; embedded distributions ship a stdlib zip instead.
  $isFullInstall = Test-Path -LiteralPath (Join-Path $srcDir 'Lib\os.py') -PathType Leaf

  if ($isFullInstall) {
    Write-Log INFO "copying full portable interpreter (excluding site-packages/pip/tcl) from $srcDir"
    $excludeTop = @(
      (Join-Path $srcDir 'Lib\site-packages'),
      (Join-Path $srcDir 'Lib\test'),
      (Join-Path $srcDir 'Scripts'),
      (Join-Path $srcDir 'share'),
      (Join-Path $srcDir 'tcl'),
      (Join-Path $srcDir 'include'),
      (Join-Path $srcDir 'libs'),
      (Join-Path $srcDir 'tkinter'),
      (Join-Path $srcDir 'tkinter_embed')
    )
    & robocopy $srcDir $runtimeDir /E /NFL /NDL /NJH /NJS /NP /XD $excludeTop /XF *.pyc | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed (exit $LASTEXITCODE)" 1 }
    $siteRoot = Join-Path $runtimeDir 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'python.exe') -PathType Leaf)) { Fail 'runtime copy produced no python.exe' 1 }
  } else {
    Write-Log INFO "copying embedded interpreter files (stdlib zip + extension modules) from $srcDir"
    $rootSkip = @('get-pip.py', 'pip.pyz', 'python.cat')
    Get-ChildItem -LiteralPath $srcDir -File -Force | Where-Object {
      $rootSkip -notcontains $_.Name -and
      $_.Name -notmatch '^(_tkinter\.pyd|tcl86t\.dll|tk86t\.dll)$'
    } | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $runtimeDir -Force }
    $siteRoot = Join-Path $runtimeDir 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'python.exe') -PathType Leaf)) { Fail 'runtime copy produced no python.exe' 1 }
  }
  New-Item -ItemType Directory -Force -Path $siteRoot | Out-Null

  $runtimePython = Join-Path $runtimeDir 'python.exe'
  Write-Log STEP 'step 3/6 - installing base engine dependencies into the bundled runtime'
  $pyproject = Join-Path $engineSrc 'pyproject.toml'
  $pkgCopied = $false
  if (Test-Path -LiteralPath $pyproject -PathType Leaf) {
    $spec = $engineSrc
    if ($WithAudioSeal) { $spec = "$engineSrc[ai]" }
    $buildArgs = @('--disable-pip-version-check', '--no-warn-script-location', '--target', $siteRoot)
    if (Test-PythonModule $pyExe 'setuptools') { $buildArgs += '--no-build-isolation' }
    Write-Log INFO "pip install --target $siteRoot $spec (engine package + base dependencies)"
    & $pyExe -m pip install @buildArgs $spec
    if ($LASTEXITCODE -ne 0) { Fail "pip install of the engine package failed (exit $LASTEXITCODE)" 1 }
    $pkgCopied = $true
  } else {
    $reqArgs = @()
    if (Test-Path -LiteralPath $baseReq -PathType Leaf) { $reqArgs += @('-r', $baseReq) }
    else { Write-Log WARN "requirements.txt missing - installing baseline packages"; $reqArgs += @('numpy', 'scipy', 'soundfile', 'librosa', 'cryptography', 'pyloudnorm') }
    if ($WithAudioSeal -and (Test-Path -LiteralPath $optionalReq -PathType Leaf)) { $reqArgs += @('-r', $optionalReq) }
    Write-Log INFO "pip install --target $siteRoot $($reqArgs -join ' ')"
    & $pyExe -m pip install --disable-pip-version-check --no-warn-script-location --target $siteRoot @reqArgs
    if ($LASTEXITCODE -ne 0) { Fail "pip --target failed (exit $LASTEXITCODE)" 1 }
  }

  if (-not $pkgCopied) {
    Write-Log INFO 'copying nl_vo_shield package into the bundled site-packages'
    $pkgDest = Join-Path $siteRoot 'nl_vo_shield'
    New-Item -ItemType Directory -Force -Path $pkgDest | Out-Null
    & robocopy $enginePkg $pkgDest /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy of the engine package failed (exit $LASTEXITCODE)" 1 }
  }

  Write-Log STEP 'step 4/6 - verifying the bundled runtime'
  $verifyImports = 'sys, numpy, scipy, soundfile, cryptography, nl_vo_shield'
  if ($WithAudioSeal) { $verifyImports = 'sys, numpy, scipy, soundfile, cryptography, nl_vo_shield, torch, audioseal' }
  $runtimeVer = (& $runtimePython -c "import $verifyImports; print('RUNTIME_OK', sys.version.split()[0], getattr(nl_vo_shield, '__version__', '?'))" 2>&1 | Out-String).Trim()
  if ($LASTEXITCODE -ne 0) { Write-Log ERROR $runtimeVer; Fail 'bundled runtime cannot import the engine + base dependencies' 3 }
  Write-Log OK "bundled runtime: $runtimeVer"
  Set-Content -LiteralPath $marker -Value "NL VO Shield generated engine payload - safe to delete and rebuild.`nDo not commit.`nruntime: $runtimeVer" -Encoding ASCII
}

# ----------------------------------------------------------- PyInstaller
$frozenExe = $null
$health = @()
if (-not $SkipPyInstaller) {
  Write-Log STEP 'step 5/6 - PyInstaller one-folder frozen engine'
  if (Test-Path -LiteralPath $cacheDir) { Remove-Item -LiteralPath $cacheDir -Recurse -Force -ErrorAction SilentlyContinue }
  New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
  $entry = Join-Path $root 'tooling\sidecar_entry.py'
  if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { Fail "missing sidecar entry script: $entry" 2 }
  $frozenDir = Join-Path $engineDir 'frozen'
  $excludeModules = @('torch', 'torchaudio', 'torchvision', 'audioseal', 'transformers', 'matplotlib', 'IPython', 'notebook', 'tkinter')
  if ($WithAudioSeal) { $excludeModules = @('matplotlib', 'IPython', 'notebook', 'tkinter') }
  $pyArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
    '--name', 'nlvo-engine',
    '--distpath', $frozenDir,
    '--workpath', $cacheDir,
    '--specpath', $cacheDir,
    '--paths', $engineSrc,
    '--collect-submodules', 'nl_vo_shield'
  )
  foreach ($module in $excludeModules) { $pyArgs += @('--exclude-module', $module) }
  $pyArgs += $entry
  Write-Log INFO ('PyInstaller excludes: ' + ($excludeModules -join ', '))
  & $pyExe @pyArgs
  if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed (exit $LASTEXITCODE)" 1 }
  $frozenExe = Join-Path $frozenDir 'nlvo-engine\nlvo-engine.exe'
  if (-not (Test-Path -LiteralPath $frozenExe -PathType Leaf)) { Fail "PyInstaller produced no executable: $frozenExe" 1 }
  Write-Log OK "frozen engine: $frozenExe ($(Format-Size (Get-Item -LiteralPath $frozenExe).Length))"
  if (-not $KeepWork -and (Test-Path -LiteralPath $cacheDir)) { Remove-Item -LiteralPath $cacheDir -Recurse -Force -ErrorAction SilentlyContinue }
}

# ------------------------------------------------------------- health checks
function Test-SidecarCli {
  param([string]$Exe, [string]$Arguments = '', [string]$Description)
  $argList = @()
  if ($Arguments) { $argList = $Arguments.Split(' ') }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $Exe @argList --version 2>&1
    $code = $LASTEXITCODE
  } finally { $ErrorActionPreference = $prev }
  $display = if ($Arguments) { "$Exe $Arguments --version" } else { "$Exe --version" }
  return @{ Description = $Description; Command = $display; Ok = ($code -eq 0); Output = (($out | Out-String).Trim()) }
}

function Test-RpcHealth {
  param([string]$Exe, [string]$Arguments, [string]$Description)
  $dataDir = Join-Path $root '.local\tmp\rpc-health'
  New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $Exe
  $psi.Arguments = "$Arguments --data-dir `"$dataDir`""
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $proc = $null
  try {
    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.StandardInput.WriteLine('{"jsonrpc":"2.0","id":1,"method":"health","params":{}}')
    $proc.StandardInput.Flush()
    $task = $proc.StandardOutput.ReadLineAsync()
    $done = $task.Wait(20000)
    if (-not $done) { return @{ Description = $Description; Command = "health via $Exe $Arguments"; Ok = $false; Output = 'timeout waiting for health response' } }
    $line = $task.Result
    $ok = $false
    try { $obj = $line | ConvertFrom-Json; $ok = ($obj.result.version -ne $null) } catch { $ok = $false }
    return @{ Description = $Description; Command = "health via $Exe $Arguments"; Ok = $ok; Output = [string]$line }
  } catch {
    return @{ Description = $Description; Command = "health via $Exe $Arguments"; Ok = $false; Output = $_.Exception.Message }
  } finally {
    if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
  }
}

Write-Log STEP 'step 6/6 - sidecar health checks'
if (-not $SkipRuntime) {
  $runtimePython = Join-Path $engineDir 'runtime\python.exe'
  $health += Test-SidecarCli -Exe $runtimePython -Arguments '-m nl_vo_shield.cli' -Description 'bundled runtime CLI (--version)'
  $health += Test-RpcHealth -Exe $runtimePython -Arguments '-m nl_vo_shield.rpc' -Description 'bundled runtime RPC health'
}
if (-not $SkipPyInstaller -and $frozenExe) {
  $health += Test-SidecarCli -Exe $frozenExe -Description 'frozen engine CLI (--version)'
  $health += Test-RpcHealth -Exe $frozenExe -Arguments '--rpc' -Description 'frozen engine RPC health'
}
foreach ($item in $health) {
  if ($item.Ok) { Write-Log OK "$($item.Description): $($item.Output)" }
  else { Write-Log WARN "$($item.Description) FAILED: $($item.Output)" }
}

# ------------------------------------------------------------- manifest
# The manifest is shipped inside the installer resources: it must never contain
# absolute developer paths, full build-host paths or health payloads with paths.
$reqHash = ''
if (Test-Path -LiteralPath $baseReq) { $reqHash = (Get-FileHash -LiteralPath $baseReq -Algorithm SHA256).Hash.ToLowerInvariant() }
elseif (Test-Path -LiteralPath (Join-Path $engineSrc 'pyproject.toml')) { $reqHash = (Get-FileHash -LiteralPath (Join-Path $engineSrc 'pyproject.toml') -Algorithm SHA256).Hash.ToLowerInvariant() }
$manifest = [ordered]@{
  schema = 1
  product = 'NL VO Shield'
  platform = "windows-$winArch"
  generated_at = (Get-Date).ToString('o')
  with_audio_seal = [bool]$WithAudioSeal
  runtime = (-not $SkipRuntime)
  pyinstaller = (-not $SkipPyInstaller)
  python = @{ source = $pyKind; version = $pyVersion }
  requirements_sha256 = $reqHash
  health = @($health | ForEach-Object { @{ description = $_.Description; ok = [bool]$_.Ok } })
  size_bytes = (Get-DirSize -Path $engineDir)
  expected_layout = @('runtime/python.exe (-m nl_vo_shield.rpc) [Windows]', 'runtime/bin/python3 [macOS/Linux]', 'frozen/nlvo-engine/nlvo-engine(.exe)')
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $engineDir 'build-manifest.json') -Encoding UTF8

$failed = @($health | Where-Object { -not $_.Ok })
$passed = @($health | Where-Object { $_.Ok })
if ($health.Count -gt 0 -and $passed.Count -eq 0) { Fail 'no sidecar entry point passed a health check' 3 }

$total = Get-DirSize -Path $engineDir
Write-Log OK "engine payload ready: $engineDir ($(Format-Size $total))"
Write-Log INFO "manifest: $(Join-Path $engineDir 'build-manifest.json')"
Write-Log INFO "log: $($script:LogFile)"
exit 0
