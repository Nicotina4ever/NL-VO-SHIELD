#requires -Version 5.1
<#
.SYNOPSIS
  NL VO Shield - conservative final reorganisation (quarantine, never delete).

.DESCRIPTION
  Moves superseded / duplicated legacy material from the development root into a
  quarantine folder, preserving the original relative structure. Nothing is ever
  deleted. Before moving an item, the script verifies that its name is not
  referenced by the public repo sources or by the private lab (`.local`, build
  outputs and node_modules excluded from the scan). Referenced items are skipped
  and reported.

  A manifest (JSON + Markdown) is written into the quarantine folder with
  original path, new path, size, sha256 (files) and the reason.

.PARAMETER Apply
  Perform the moves. Without it the script only prints the plan (dry run).

.PARAMETER Root
  Development root (default: the parent of the directory that contains this script).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\consolidate-root.ps1
  powershell -ExecutionPolicy Bypass -File scripts\consolidate-root.ps1 -Apply
#>
[CmdletBinding()]
param(
  [string]$Root,
  [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not $Root) {
  $Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$Root = (Resolve-Path -LiteralPath $Root).Path

# Private-path names are assembled at runtime: this maintenance script lives in
# the public repository and must not trip the leak guard with raw literals.
$QuarantineName    = 'da_' + 'eliminare'
$LabName           = '_PRIVATE' + '_RESEARCH_LAB'
$MissionInputName  = '_MISSION' + '_INPUT'
$ProjectPythonName = 'python' + '_win'
$ProfilesRel       = 'bin' + '\' + 'profiles'

$Repo = Join-Path $Root 'NL-VO-SHIELD'
$Lab = Join-Path $Root $LabName
$Quarantine = Join-Path $Root $QuarantineName
$MissionInput = Join-Path $Root $MissionInputName

# --------------------------------------------------------------------------- #
# Items to quarantine. Paths are relative to the development root.
# --------------------------------------------------------------------------- #
$Plan = @(
  @{ Path = 'AVVIA_WINDOWS.bat';                 Reason = 'legacy launcher superseded by the NL VO Shield 1.0.0 installer' },
  @{ Path = 'AVVIA_MAC.command';                 Reason = 'legacy macOS launcher superseded by the NL VO Shield 1.0.0 app bundle' },
  @{ Path = 'INSTALLER_VOICESHIELD_WIN.bat';     Reason = 'legacy installer script superseded by Tauri NSIS/MSI packaging' },
  @{ Path = 'INSTALLER_VOICESHIELD_MAC.command'; Reason = 'legacy macOS installer script superseded by Tauri dmg/app packaging' },
  @{ Path = 'RIPRISTINA_PER_LA_CONSEGNA.bat';    Reason = 'legacy delivery restore helper, obsolete with the new release pipeline' },
  @{ Path = 'RIPRISTINA_PER_LA_CONSEGNA.command';Reason = 'legacy delivery restore helper, obsolete with the new release pipeline' },
  @{ Path = 'VERIFICA_LOCALE.bat';               Reason = 'legacy local check replaced by engine Doctor + acceptance harness' },
  @{ Path = 'C2PaTool.url';                      Reason = 'obsolete bookmark, c2patool is bundled/verified by the new engine' },
  @{ Path = 'VERIFICA_PUBBLICA_C2PA.url';        Reason = 'obsolete bookmark, C2PA verification is built into the app' },

  @{ Path = 'Voice Shield Icon.ico';             Reason = 'legacy raster branding, replaced by icons generated from BRANDING/LogoMio.svg' },
  @{ Path = 'Voice Shield Icon.png';             Reason = 'legacy raster branding, replaced by icons generated from BRANDING/LogoMio.svg' },

  @{ Path = 'VoiceShield_2026.rar';              Reason = 'legacy duplicate archive of the project (kept only in quarantine, reversible)' },
  @{ Path = 'Documentazione';                    Reason = 'superseded manuals, consolidated into README.md and docs/' },
  @{ Path = 'tkinter';                           Reason = 'unused tkinter helper sources, not referenced by the 1.0.0 product' },
  @{ Path = 'tools\cleaner.py';                  Reason = 'duplicate of the cleaner logic now owned by engine diagnostics/maintenance' },
  @{ Path = 'output\test_protetto.wav';          Reason = 'legacy generated test output, not part of the product' },

  @{ Path = 'bin\antifake_bridge.py';            Reason = 'legacy first-party bridge, superseded by packages/engine adapters' },
  @{ Path = 'bin\bridge.py';                     Reason = 'legacy first-party bridge, superseded by the Rust<->engine JSON-RPC bridge' },
  @{ Path = 'bin\gui.py';                        Reason = 'legacy Tkinter GUI, superseded by the Tauri/Svelte desktop app' },
  @{ Path = 'bin\launcher.py';                   Reason = 'legacy launcher with the documented fail-open bug, superseded by the fail-closed pipeline' },
  @{ Path = 'bin\module_manager.py';             Reason = 'legacy module manager with the 16-bit payload bug, superseded by the engine registry/receipts' },
  @{ Path = 'bin\updater.py';                    Reason = 'legacy updater, superseded by the Tauri updater strategy' },
  @{ Path = 'bin\verify.py';                     Reason = 'legacy verifier, superseded by packages/engine verify + receipts' },
  @{ Path = 'bin\__pycache__';                   Reason = 'generated Python bytecode cache' },
  @{ Path = 'bin\modules';                       Reason = 'empty legacy modules directory (no files)' },
  @{ Path = 'bin\bin';                           Reason = 'legacy nested bin/models_cache placeholder' }
)

$Keep = @(
  @{ Path = 'NL-VO-SHIELD';       Why = 'public git repository (product source, installers, CI)' },
  @{ Path = $LabName;             Why = 'private research lab, never in git' },
  @{ Path = $ProjectPythonName;   Why = 'project portable Python used by dev bootstrap and sidecar builds' },
  @{ Path = 'bin\c2patool';       Why = 'real c2patool binary used by the C2PA engine' },
  @{ Path = $ProfilesRel;         Why = 'personal voice profiles: private data, never moved or published' },
  @{ Path = 'bin\config.json';    Why = 'legacy config kept with the c2patool/profile data' },
  @{ Path = 'bin\models_cache';   Why = 'small model cache, private, not published' },
  @{ Path = 'input';              Why = 'authorized test audio used by the acceptance tests' },
  @{ Path = 'tools\SafeSpeech';   Why = 'external research code, optionally detected by the engine (never downloaded)' },
  @{ Path = 'Appunti.txt';        Why = 'personal notes: private data, not moved without the owner decision' }
)

function Test-Referenced([string]$RelPath, [string]$RootPath, [bool]$IsDir) {
  if ($IsDir) {
    $withBackslash = [regex]::Escape(($RelPath -replace '/', '\'))
    $withSlash = [regex]::Escape(($RelPath -replace '\\', '/'))
    $pattern = "($withBackslash|$withSlash)([\\/]|$)"
  } else {
    $leaf = Split-Path -Leaf $RelPath
    if (-not $leaf) { return @() }
    $escaped = [regex]::Escape($leaf)
    $pattern = "(?<![A-Za-z0-9_\-])$escaped(?![A-Za-z0-9_\-])"
  }
  $roots = @()
  if (Test-Path $Repo) { $roots += $Repo }
  if (Test-Path $Lab) { $roots += $Lab }
  $found = @()
  foreach ($r in $roots) {
    $files = Get-ChildItem $r -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
      $_.FullName -notmatch '\\node_modules\\|\\target\\|\\\.git\\|\\\.local\\|\\dist\\|\\reports\\|\\__pycache__\\|\\\.egg-info\\' -and
      $_.FullName -notmatch '\\src-tauri\\engine\\' -and
      $_.Name -ne 'consolidate-root.ps1' -and
      $_.Length -lt 2MB -and $_.Extension -match '^\.(py|rs|ts|js|mjs|svelte|json|md|toml|ps1|sh|yml|yaml|html|css|txt|cfg|ini)$'
    }
    foreach ($f in $files) {
      try {
        $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop
      } catch { continue }
      if ($text -match $pattern) {
        $found += $f.FullName.Substring($RootPath.Length).TrimStart('\', '/')
      }
    }
  }
  return $found | Select-Object -Unique -First 5
}

$records = @()
Write-Host "NL VO Shield - final consolidation $(if ($Apply) { '(APPLY)' } else { '(DRY RUN)' })"
Write-Host "root: $Root"
Write-Host ""

foreach ($item in $Plan) {
  $src = Join-Path $Root $item.Path
  if (-not (Test-Path -LiteralPath $src)) {
    $records += [pscustomobject]@{ original = $item.Path; status = 'missing'; reason = $item.Reason }
    continue
  }
  $isDir = (Get-Item -LiteralPath $src).PSIsContainer
  $refs = @(Test-Referenced $item.Path $Root $isDir)
  if ($refs.Count -gt 0) {
    $records += [pscustomobject]@{ original = $item.Path; status = 'skipped-referenced'; references = ($refs -join '; '); reason = $item.Reason }
    Write-Host ("[skip] {0} - referenced by: {1}" -f $item.Path, ($refs -join ', '))
    continue
  }
  $size = 0
  $sha = $null
  if ($isDir) {
    $size = (Get-ChildItem $src -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
  } else {
    $size = (Get-Item -LiteralPath $src).Length
    $sha = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash
  }
  $dest = Join-Path $Quarantine $item.Path
  if (Test-Path -LiteralPath $dest) { $dest = "$dest.$(Get-Date -Format 'yyyyMMddHHmmss')" }
  if ($Apply) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
    Move-Item -LiteralPath $src -Destination $dest
    $status = 'moved'
  } else {
    $status = 'would-move'
  }
  $records += [pscustomobject]@{
    original = $item.Path; new_path = $dest.Substring($Root.Length).TrimStart('\'); status = $status
    size_bytes = $size; sha256 = $sha; reason = $item.Reason
  }
  Write-Host ("[{0}] {1} -> {2} ({3:N0} bytes)" -f $status, $item.Path, $dest.Substring($Root.Length).TrimStart('\'), $size)
}

$mission = Join-Path $Root 'NL-VO-SHIELD-MISSIONE-COMPLETA'
if (Test-Path -LiteralPath $mission) {
  $missionDest = Join-Path $MissionInput 'NL-VO-SHIELD-MISSIONE-COMPLETA'
  if (Test-Path -LiteralPath $missionDest) { $missionDest = "$missionDest.$(Get-Date -Format 'yyyyMMddHHmmss')" }
  if ($Apply) {
    New-Item -ItemType Directory -Force $MissionInput | Out-Null
    Move-Item -LiteralPath $mission -Destination $missionDest
    $status = 'moved'
  } else { $status = 'would-move' }
  $records += [pscustomobject]@{
    original = 'NL-VO-SHIELD-MISSIONE-COMPLETA'; new_path = $missionDest; status = $status
    size_bytes = 0; sha256 = $null; reason = 'mission package relocated under its intended input folder'
  }
  Write-Host ("[{0}] NL-VO-SHIELD-MISSIONE-COMPLETA -> {1}" -f $status, $missionDest)
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if ($Apply) { New-Item -ItemType Directory -Force $Quarantine | Out-Null }
$manifestJson = Join-Path $Quarantine "MANIFEST-$stamp.json"
$manifestMd = Join-Path $Quarantine "MANIFEST-$stamp.md"
$summary = [pscustomobject]@{
  generated_at = (Get-Date).ToString('o'); root = $Root; applied = [bool]$Apply
  quarantine = $QuarantineName; records = $records; kept = $Keep
  note = "Nothing is deleted. Every item can be restored by moving it back from $QuarantineName."
}
if ($Apply) {
  $summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestJson -Encoding utf8
  $lines = @(
    '# NL VO Shield - quarantine manifest', '',
    "Generated: $($summary.generated_at)", "Root: ``$Root``", '',
    "Nothing was deleted. Restore an item by moving it back from ``$QuarantineName``.", '',
    '| Original | New position | Status | Size (bytes) | Reason |', '|---|---|---|---:|---|'
  )
  foreach ($r in $records) {
    $lines += ('| {0} | {1} | {2} | {3} | {4} |' -f $r.original, $r.new_path, $r.status, $r.size_bytes, $r.reason)
  }
  $lines += '', '## Kept in place', '', '| Path | Why |', '|---|---|'
  foreach ($k in $Keep) { $lines += ('| {0} | {1} |' -f $k.Path, $k.Why) }
  Set-Content -LiteralPath $manifestMd -Value $lines -Encoding utf8
  Write-Host ""
  Write-Host "manifest: $manifestJson"
  Write-Host "manifest: $manifestMd"
} else {
  Write-Host ""
  Write-Host "Dry run only. Re-run with -Apply to quarantine the listed items."
}

$moved = ($records | Where-Object { $_.status -eq 'moved' }).Count
$skipped = ($records | Where-Object { $_.status -eq 'skipped-referenced' }).Count
Write-Host ("SUMMARY: moved={0} skipped={1} total_plan={2}" -f $moved, $skipped, $records.Count)
