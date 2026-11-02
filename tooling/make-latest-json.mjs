#!/usr/bin/env node
/**
 * NL VO Shield - build the Tauri updater `latest.json` from signed release artifacts.
 *
 * Used by .github/workflows/release.yml ONLY when the Tauri signing key secret is
 * available. It never invents signatures: an artifact without its `.sig` file is
 * skipped, and no platform entry is emitted for it.
 *
 * Usage:
 *   node tooling/make-latest-json.mjs --dist <dir> --version 0.1.0 \
 *     --repo Nicotina4ever/NL-VO-SHIELD [--tag v0.1.0] [--out latest.json]
 *
 * Platform inference (Tauri v2 updater targets):
 *   *.exe.sig                       -> windows-x86_64
 *   *.msi.sig                       -> windows-x86_64 (Windows installer)
 *   *.app.tar.gz.sig                -> darwin-<arch from filename>
 *   *.AppImage.tar.gz.sig           -> linux-x86_64
 *   *.AppImage.sig                  -> linux-x86_64
 *   *.deb.sig / *.rpm.sig           -> (not used by the updater; ignored)
 *
 * Exit codes: 0 written, 1 no signature found, 2 bad usage.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

function findRoot(start) {
  let dir = path.resolve(start);
  for (;;) {
    if (fs.existsSync(path.join(dir, 'assets', 'LogoMio.svg'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function parseArgs(argv) {
  const opts = { dist: null, version: '', repo: 'Nicotina4ever/NL-VO-SHIELD', tag: '', out: '' };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dist') opts.dist = path.resolve(argv[++i]);
    else if (a === '--version') opts.version = argv[++i];
    else if (a === '--repo') opts.repo = argv[++i];
    else if (a === '--tag') opts.tag = argv[++i];
    else if (a === '--out') opts.out = path.resolve(argv[++i]);
    else if (a === '--help' || a === '-h') {
      console.log('usage: node tooling/make-latest-json.mjs --dist <dir> --version <v> [--repo owner/name] [--tag vX] [--out file]');
      process.exit(0);
    } else {
      console.error(`[latest-json] unknown argument: ${a}`);
      process.exit(2);
    }
  }
  return opts;
}

const opts = parseArgs(process.argv.slice(2));
if (!opts.dist || !opts.version) {
  console.error('[latest-json] ERROR: --dist and --version are required');
  process.exit(2);
}
const root = findRoot(SCRIPT_DIR) || path.resolve(opts.dist, '..');
const tag = opts.tag || `v${opts.version}`;
const outFile = opts.out || path.join(opts.dist, 'latest.json');

function walk(dir, acc = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else if (entry.isFile()) acc.push(full);
  }
  return acc;
}

function platformFor(baseName) {
  const name = baseName.toLowerCase();
  // Tauri v2 updater targets: windows-x86_64, darwin-aarch64, darwin-x86_64, linux-x86_64.
  if (name.endsWith('.exe')) return 'windows-x86_64';
  if (name.endsWith('.msi')) return 'windows-x86_64';
  if (name.endsWith('.app.tar.gz')) {
    if (/(aarch64|arm64)/.test(name)) return 'darwin-aarch64';
    if (/(x64|x86_64|intel)/.test(name)) return 'darwin-x86_64';
    return 'darwin-aarch64';
  }
  if (name.endsWith('.appimage.tar.gz') || name.endsWith('.appimage')) {
    return /(aarch64|arm64)/.test(name) ? 'linux-aarch64' : 'linux-x86_64';
  }
  if (name.endsWith('.tar.gz') && /(x64|x86_64|aarch64|arm64)/.test(name) && /shield/.test(name)) {
    return /(aarch64|arm64)/.test(name) ? 'darwin-aarch64' : 'darwin-x86_64';
  }
  return null;
}

if (!fs.existsSync(opts.dist)) {
  console.error(`[latest-json] ERROR: dist directory not found: ${opts.dist}`);
  process.exit(2);
}

const files = walk(opts.dist);
const platforms = {};
const duplicates = [];
for (const file of files) {
  if (!file.endsWith('.sig')) continue;
  const artifact = file.slice(0, -4);
  if (!fs.existsSync(artifact)) {
    console.warn(`[latest-json] WARN: signature without artifact: ${file}`);
    continue;
  }
  const base = path.basename(artifact);
  const platform = platformFor(base);
  if (!platform) {
    console.log(`[latest-json] skip (not an updater artifact): ${base}`);
    continue;
  }
  if (platforms[platform]) {
    duplicates.push(`${platform}: ${platforms[platform].file} vs ${base}`);
    continue;
  }
  const signature = fs.readFileSync(file, 'utf8').trim();
  if (!signature) {
    console.warn(`[latest-json] WARN: empty signature: ${file}`);
    continue;
  }
  platforms[platform] = {
    signature,
    url: `https://github.com/${opts.repo}/releases/download/${tag}/${encodeURIComponent(base).replace(/%2F/g, '/')}`,
    file: base,
  };
}

if (Object.keys(platforms).length === 0) {
  console.error('[latest-json] ERROR: no signed updater artifact found - refusing to write latest.json');
  process.exit(1);
}
if (duplicates.length) {
  console.error('[latest-json] ERROR: duplicate updater artifacts for the same platform:');
  for (const d of duplicates) console.error('  ' + d);
  process.exit(1);
}

const output = {
  version: opts.version,
  notes: `NL VO Shield ${opts.version} - see the release notes on GitHub.`,
  pub_date: new Date().toISOString(),
  platforms: Object.fromEntries(
    Object.entries(platforms)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => [k, { signature: v.signature, url: v.url }]),
  ),
};
fs.writeFileSync(outFile, JSON.stringify(output, null, 2) + '\n');
console.log(`[latest-json] wrote ${outFile}`);
for (const [platform, info] of Object.entries(output.platforms)) {
  console.log(`  ${platform}: ${info.url}`);
}
