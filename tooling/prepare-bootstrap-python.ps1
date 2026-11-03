# Prepare the embedded bootstrap Python shipped inside the installer.
# It is copied under <install>/engine/bootstrap and is the ONLY interpreter the
# app may use to create the optional AI runtime venv. The system Python is never used.
param(
    [string]$Destination,
    [string]$CacheRoot = ""
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $CacheRoot) { $CacheRoot = Join-Path $root '.local\cache\bootstrap-python' }
if (-not $Destination) { $Destination = Join-Path $root 'apps\desktop\src-tauri\engine\bootstrap' }

$asset = 'cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only.tar.gz'
$sha = '75cb501dc11c7b55c6b447c58e4910bce5b1f609f1017328b4f3a858dfaf5428'
$url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.11.18%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz'
$work = Join-Path $CacheRoot 'windows-x64'
$pythonDir = Join-Path $work 'python'
$getPip = Join-Path $work 'get-pip.py'

New-Item -ItemType Directory -Force $work | Out-Null
if (-not (Test-Path (Join-Path $pythonDir 'python.exe'))) {
    $archive = Join-Path $work $asset
    if (-not (Test-Path $archive)) {
        Write-Host "[bootstrap] download $asset"
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    }
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $sha) { throw "bootstrap python checksum mismatch: $actual != $sha" }
    if (Test-Path $pythonDir) { Remove-Item $pythonDir -Recurse -Force }
    tar -xzf $archive -C $work
    if (-not (Test-Path (Join-Path $pythonDir 'python.exe'))) { throw "bootstrap archive layout unexpected" }
}
if (-not (Test-Path $getPip)) {
    Write-Host '[bootstrap] download get-pip.py'
    Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
}
if (Test-Path $Destination) { Remove-Item $Destination -Recurse -Force }
New-Item -ItemType Directory -Force $Destination | Out-Null
Copy-Item $pythonDir (Join-Path $Destination 'python') -Recurse -Force
Copy-Item $getPip (Join-Path $Destination 'get-pip.py') -Force
# Remove training/test/ensurepip payloads: get-pip.py is provided explicitly and
# the venv is created with --without-pip. This keeps the installer small and
# avoids platform launcher .exe files inside the Linux/macOS bootstrap.
$pyRoot = Join-Path $Destination 'python'
foreach ($relative in @('Lib\test','Lib\ensurepip','Lib\site-packages','Lib\idlelib','Lib\tkinter','share','include','libs')) {
  $target = Join-Path $pyRoot $relative
  if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
}
$size = (Get-ChildItem $Destination -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("[bootstrap] embedded Python ready: {0} ({1:N1} MB)" -f $Destination, ($size / 1MB))


