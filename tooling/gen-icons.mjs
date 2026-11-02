#!/usr/bin/env node
/**
 * NL VO Shield — icon generator.
 *
 * Generates the multi-OS application icons from the untouched brand source
 * `assets/LogoMio.svg` using sharp (SVG rasterisation) + the Tauri CLI
 * (`tauri icon`, which also produces .icns even when running on Windows).
 *
 * Usage:
 *   node tooling/gen-icons.mjs            # generate assets/icons/*
 *   node tooling/gen-icons.mjs --check    # verify existing icons only
 *   node tooling/gen-icons.mjs --out <dir> --source <svg>
 *
 * Exit codes: 0 = ok, 1 = generation/verification failed, 2 = prerequisites missing.
 */
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
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

const ROOT = findRoot(SCRIPT_DIR);
if (!ROOT) {
  console.error('[gen-icons] ERROR: repository root not found (assets/LogoMio.svg missing while ascending from ' + SCRIPT_DIR + ')');
  process.exit(2);
}

// ---------------------------------------------------------------- arguments
function parseArgs(argv) {
  const opts = {
    out: path.join(ROOT, 'assets', 'icons'),
    source: path.join(ROOT, 'assets', 'LogoMio.svg'),
    check: false,
    force: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--check') opts.check = true;
    else if (a === '--force') opts.force = true;
    else if (a === '--out') opts.out = path.resolve(argv[++i]);
    else if (a === '--source') opts.source = path.resolve(argv[++i]);
    else if (a === '--help' || a === '-h') {
      console.log('Usage: node tooling/gen-icons.mjs [--check] [--out DIR] [--source SVG] [--force]');
      process.exit(0);
    } else {
      console.error(`[gen-icons] unknown argument: ${a}`);
      process.exit(2);
    }
  }
  return opts;
}

const OPTIONS = parseArgs(process.argv.slice(2));

// ------------------------------------------------------------- icon checks
const REQUIRED = [
  'icon.ico',
  'icon.icns',
  'icon.png',
  '32x32.png',
  '128x128.png',
  '128x128@2x.png',
];
const STORE_GLOBS = ['Square30x30Logo.png', 'Square44x44Logo.png', 'Square150x150Logo.png', 'StoreLogo.png'];

function readPngSize(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const head = Buffer.alloc(26);
    const read = fs.readSync(fd, head, 0, 26, 0);
    if (read < 26) return null;
    const sig = head.subarray(0, 8).toString('hex');
    if (sig !== '89504e470d0a1a0a') return null;
    if (head.subarray(12, 16).toString('ascii') !== 'IHDR') return null;
    return { width: head.readUInt32BE(16), height: head.readUInt32BE(20) };
  } finally {
    fs.closeSync(fd);
  }
}

function readIcoInfo(file) {
  const buf = fs.readFileSync(file);
  if (buf.length < 6) return null;
  if (buf.readUInt16LE(0) !== 0 || buf.readUInt16LE(2) !== 1) return null;
  const count = buf.readUInt16LE(4);
  if (count === 0) return null;
  const sizes = [];
  for (let i = 0; i < Math.min(count, 32); i++) {
    const off = 6 + i * 16;
    if (off + 16 > buf.length) break;
    const w = buf[off] === 0 ? 256 : buf[off];
    const h = buf[off + 1] === 0 ? 256 : buf[off + 1];
    sizes.push(`${w}x${h}`);
  }
  return { count, sizes };
}

function readIcnsInfo(file) {
  const buf = fs.readFileSync(file);
  if (buf.length < 8) return null;
  if (buf.subarray(0, 4).toString('ascii') !== 'icns') return null;
  const declared = buf.readUInt32BE(4);
  const types = [];
  let off = 8;
  while (off + 8 <= buf.length && types.length < 64) {
    const type = buf.subarray(off, off + 4).toString('ascii');
    const len = buf.readUInt32BE(off + 4);
    if (len < 8 || off + len > buf.length) break;
    types.push(type);
    off += len;
  }
  return { declared, actual: buf.length, types };
}

function verifyIcons(dir, { quiet = false } = {}) {
  const problems = [];
  const found = [];
  if (!fs.existsSync(dir)) {
    return { ok: false, problems: [`output directory missing: ${dir}`], found };
  }
  for (const name of REQUIRED) {
    const file = path.join(dir, name);
    if (!fs.existsSync(file)) {
      problems.push(`missing required icon: ${name}`);
      continue;
    }
    const size = fs.statSync(file).size;
    if (size <= 0) {
      problems.push(`empty icon: ${name}`);
      continue;
    }
    let detail = `${size} bytes`;
    if (name.endsWith('.png')) {
      const png = readPngSize(file);
      if (!png) problems.push(`invalid PNG header: ${name}`);
      else detail += ` ${png.width}x${png.height}`;
    } else if (name.endsWith('.ico')) {
      const ico = readIcoInfo(file);
      if (!ico) problems.push(`invalid ICO header: ${name}`);
      else detail += ` entries=${ico.count} sizes=${ico.sizes.slice(0, 4).join(',')}`;
    } else if (name.endsWith('.icns')) {
      const icns = readIcnsInfo(file);
      if (!icns) problems.push(`invalid ICNS header: ${name}`);
      else if (icns.declared !== icns.actual) problems.push(`ICNS length mismatch: ${name} declared=${icns.declared} actual=${icns.actual}`);
      else detail += ` types=${icns.types.length} (${icns.types.slice(0, 4).join(',')})`;
    }
    found.push({ name, size, detail });
  }
  // Windows Store squares are optional but produced by `tauri icon` on all hosts.
  const store = STORE_GLOBS.filter((n) => fs.existsSync(path.join(dir, n)));
  if (store.length > 0 && store.length !== STORE_GLOBS.length) {
    problems.push(`incomplete Windows Store icon set (found ${store.length}/${STORE_GLOBS.length})`);
  }
  if (!quiet) {
    for (const f of found) console.log(`  [ok] ${f.name.padEnd(22)} ${f.detail}`);
    if (store.length) console.log(`  [ok] Windows Store squares: ${store.length}/4`);
  }
  return { ok: problems.length === 0, problems, found, store };
}

// -------------------------------------------------------------- generation
function ensureSharp() {
  try {
    return require('sharp');
  } catch {
    return null;
  }
}

function resolveTauriCli() {
  // Points at the JS launcher shipped by @tauri-apps/cli; portable across OSes.
  const bases = [
    path.join(ROOT, 'tooling', 'node_modules'),
    path.join(ROOT, 'apps', 'desktop', 'node_modules'),
    path.join(ROOT, 'node_modules'),
  ];
  for (const base of bases) {
    const pkgRoot = path.join(base, '@tauri-apps', 'cli');
    const launcher = path.join(pkgRoot, 'tauri.js');
    if (fs.existsSync(launcher)) return launcher;
  }
  // Fall back to module resolution from this script (handles hoisting).
  try {
    const pkg = require.resolve('@tauri-apps/cli/package.json');
    const launcher = path.join(path.dirname(pkg), 'tauri.js');
    if (fs.existsSync(launcher)) return launcher;
  } catch {
    /* ignore */
  }
  return null;
}

function run(cmd, args, options = {}) {
  const res = spawnSync(cmd, args, { stdio: 'inherit', ...options });
  if (res.error) throw res.error;
  return res.status ?? 1;
}

function generate() {
  if (!fs.existsSync(OPTIONS.source)) {
    console.error(`[gen-icons] ERROR: source SVG not found: ${OPTIONS.source}`);
    process.exit(2);
  }
  const tauriLauncher = resolveTauriCli();
  if (!tauriLauncher) {
    console.error('[gen-icons] ERROR: @tauri-apps/cli not installed.');
    console.error('            Run: npm --prefix tooling install   (or npm --prefix tooling ci)');
    process.exit(2);
  }

  const cacheDir = path.join(ROOT, '.local', 'cache', 'icons');
  fs.mkdirSync(cacheDir, { recursive: true });
  fs.mkdirSync(OPTIONS.out, { recursive: true });

  const sharp = ensureSharp();
  let inputPng = path.join(cacheDir, 'logo-1024.png');
  let tauriInput = OPTIONS.source;

  if (sharp) {
    console.log(`[gen-icons] rasterising ${path.relative(ROOT, OPTIONS.source)} -> ${path.relative(ROOT, inputPng)} (1024x1024)`);
    const svg = fs.readFileSync(OPTIONS.source);
    sharp(svg, { density: 384 })
      .resize(1024, 1024, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
      .png()
      .toFile(inputPng)
      .then(() => {
        const png = readPngSize(inputPng);
        if (!png) throw new Error('sharp did not produce a valid PNG');
        console.log(`[gen-icons] source raster ${png.width}x${png.height}`);
        generateWithTauri(tauriLauncher, inputPng, sharp);
      })
      .catch((err) => {
        console.error(`[gen-icons] ERROR during rasterisation: ${err.message}`);
        process.exit(1);
      });
  } else {
    console.warn('[gen-icons] sharp not installed: trying to feed the SVG directly to `tauri icon`.');
    console.warn('            For deterministic output run: npm --prefix tooling install');
    if (!/\.svg$/i.test(tauriInput)) {
      console.error('[gen-icons] ERROR: cannot rasterise a non-SVG source without sharp.');
      process.exit(2);
    }
    generateWithTauri(tauriLauncher, tauriInput, null);
  }
}

function generateWithTauri(launcher, input, sharp) {
  console.log(`[gen-icons] tauri icon -o ${path.relative(ROOT, OPTIONS.out)} ...`);
  const status = run(process.execPath, [launcher, 'icon', input, '-o', OPTIONS.out]);
  if (status !== 0) {
    console.error(`[gen-icons] ERROR: \`tauri icon\` exited with code ${status}`);
    process.exit(1);
  }
  // Extra UI-facing raster (512) from the generated master icon.
  try {
    const master = path.join(OPTIONS.out, 'icon.png');
    const uiLogo = path.join(OPTIONS.out, 'ui-logo.png');
    if (fs.existsSync(master)) {
      if (sharp) {
        sharp(master).resize(512, 512).png().toFile(uiLogo).then(() => {
          finish();
        }).catch((err) => {
          console.warn(`[gen-icons] WARN: could not create ui-logo.png: ${err.message}`);
          finish();
        });
        return;
      }
      fs.copyFileSync(master, uiLogo);
    }
  } catch (err) {
    console.warn(`[gen-icons] WARN: UI logo step failed: ${err.message}`);
  }
  finish();
}

function finish() {
  // This is a desktop (Tauri) product: drop the mobile icon sets the CLI also emits,
  // unless explicitly requested, so assets/icons stays small and auditable.
  if (!process.argv.includes('--mobile')) {
    for (const dir of ['ios', 'android']) {
      const target = path.join(OPTIONS.out, dir);
      if (fs.existsSync(target)) {
        fs.rmSync(target, { recursive: true, force: true });
        console.log(`[gen-icons] removed unused mobile icon set: ${dir}/`);
      }
    }
  }
  console.log('[gen-icons] verifying generated icons:');
  const result = verifyIcons(OPTIONS.out);
  if (!result.ok) {
    for (const p of result.problems) console.error(`  [FAIL] ${p}`);
    process.exit(1);
  }
  console.log(`[gen-icons] OK — ${result.found.length} required icons + ${result.store.length} store squares in ${path.relative(ROOT, OPTIONS.out)}`);
  process.exit(0);
}

// ------------------------------------------------------------------- main
if (OPTIONS.check) {
  console.log(`[gen-icons] --check ${path.relative(ROOT, OPTIONS.out)}`);
  const result = verifyIcons(OPTIONS.out);
  if (!result.ok) {
    for (const p of result.problems) console.error(`  [FAIL] ${p}`);
    console.error('[gen-icons] icon verification FAILED');
    process.exit(1);
  }
  console.log('[gen-icons] icon verification OK');
  process.exit(0);
}

console.log(`[gen-icons] repo root: ${ROOT}`);
generate();
