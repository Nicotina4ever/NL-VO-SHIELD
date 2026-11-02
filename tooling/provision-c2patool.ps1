#requires -Version 5.1
<#
.SYNOPSIS
  Provision c2patool v0.9.12 for BUILD-TIME packaging on Windows.

.DESCRIPTION
  Downloads the pinned official release asset, verifies its SHA-256 and copies
  the binary to <repo>\bin\c2patool\c2patool.exe (path expected by the engine).
  Nothing is downloaded at product runtime.
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$destDir = Join-Path $root 'bin\c2patool'
$url = 'https://github.com/contentauth/c2patool/releases/download/v0.9.12/c2patool-v0.9.12-x86_64-pc-windows-msvc.zip'
$sha = '729be50407ca20ad72077a729c7e233b4ff8b37b57cd28bca24fd3d5ef5315d4'
$work = Join-Path $env:TEMP "c2patool-provision-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force $work | Out-Null
try {
  $archive = Join-Path $work 'c2patool.zip'
  Write-Host '[c2patool] windows download'
  Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
  $actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $sha) { throw "c2patool checksum mismatch: $actual" }
  Expand-Archive -Path $archive -DestinationPath (Join-Path $work 'extract') -Force
  $found = Get-ChildItem -Path (Join-Path $work 'extract') -Recurse -File -Filter 'c2patool.exe' | Select-Object -First 1
  if (-not $found) { throw 'c2patool.exe not found in the archive' }
  New-Item -ItemType Directory -Force $destDir | Out-Null
  $dest = Join-Path $destDir 'c2patool.exe'
  Copy-Item $found.FullName $dest -Force
  & $dest --version
  Write-Host "[c2patool] ready: $dest"
}
finally {
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
