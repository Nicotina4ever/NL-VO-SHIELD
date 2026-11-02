#!/usr/bin/env node
/**
 * NL VO Shield — leak guard.
 *
 * Fails (exit 1) when private or non-redistributable material is about to be
 * committed or shipped:
 *   - absolute developer paths (K:\..., C:\Users\...), the private research lab,
 *     the mission folder, `da_eliminare`, the sibling project Python, `bin/profiles`;
 *   - audio recordings, private keys, model weights, executables;
 *   - credential-like / high-entropy tokens in text files.
 *
 * Modes
 *   node tooling/leak-guard.mjs                  # git tracked+staged files, or all non-ignored files
 *   node tooling/leak-guard.mjs --all            # same, explicit
 *   node tooling/leak-guard.mjs --scan <path>    # scan a file or directory (binary-safe) — used after packaging
 *   node tooling/leak-guard.mjs --self-test      # create a temporary violation, prove it is detected, delete it
 *   node tooling/leak-guard.mjs --list           # print the files that would be scanned and exit
 *
 * Exit codes: 0 clean, 1 violations found, 2 tool/setup error.
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
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

const ROOT = findRoot(SCRIPT_DIR);
if (!ROOT) {
  console.error('[leak-guard] ERROR: repository root not found while ascending from ' + SCRIPT_DIR);
  process.exit(2);
}

const toPosix = (p) => p.split(path.sep).join('/');
const rel = (p) => toPosix(path.relative(ROOT, p));

// ---------------------------------------------------------------- patterns
/** Developer paths / private material that must never be public. */
const PRIVATE_MARKERS = [
  { id: 'windows-drive-path', re: /[A-Za-z]:\\{1,2}(?:AI|Users|VoiceShield|Projects?)[\\\\/]/i, hint: 'absolute Windows developer path' },
  { id: 'mac-user-home', re: /\/Users\/[A-Za-z0-9._-]+\//, hint: 'absolute macOS user path' },
  { id: 'research-lab', re: /_PRIVATE_RESEARCH_LAB/i, hint: 'private research lab' },
  { id: 'mission-folder', re: /NL-VO-SHIELD-MISSIONE-COMPLETA|_MISSION_INPUT/i, hint: 'mission input folder' },
  { id: 'delete-bin', re: /\bda_eliminare\b/i, hint: 'quarantine folder' },
  { id: 'project-python', re: /\bpython_win\b/i, hint: 'sibling project Python runtime' },
  { id: 'profiles-dir', re: /\bbin[\\/]{1,2}profiles\b/i, hint: 'personal voice profiles' },
];

/** Executable / key-like file names that must not be tracked. */
const FORBIDDEN_EXTENSIONS = new Set([
  '.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.opus', '.aiff', '.amr', '.wma',
  '.key', '.pem', '.p12', '.pfx', '.jks', '.keystore', '.kdbx',
  '.pth', '.pt', '.ckpt', '.onnx', '.safetensors', '.gguf', '.h5', '.model', '.pkl',
  '.npy', '.npz', '.exe', '.msi', '.dmg', '.app', '.appimage', '.deb', '.rpm',
  '.snap', '.flatpak', '.dll', '.so', '.dylib', '.lib', '.a', '.o', '.pdb',
]);

/** Paths that are allowed to mention the forbidden markers, with a reason. */
const FILE_ALLOWLIST = new Map([
  ['.gitignore', 'must list the private paths it excludes'],
  ['tooling/leak-guard.mjs', 'defines and self-tests the forbidden patterns'],
  ['tooling/bootstrap.ps1', 'must locate the sibling project Python and document the fallback'],
  ['tooling/bootstrap.sh', 'must locate the portable project Python'],
  ['tooling/run.sh', 'must locate the portable project Python'],
  ['tooling/build-engine-sidecar.ps1', 'must resolve the project Python for PyInstaller'],
  ['tooling/build-engine-sidecar.sh', 'must resolve the project Python for PyInstaller'],
  ['tooling/package.ps1', 'runs the leak guard and verifies the bundle content'],
  ['tooling/package.sh', 'runs the leak guard and verifies the bundle content'],
  ['packages/engine/nl_vo_shield/paths.py', 'resolves the sibling project Python directory name required by the engine contract (relative path only, no absolute path)'],
  ['apps/desktop/src-tauri/src/paths.rs', 'resolves the sibling project Python distribution next to the repo (relative path only, no absolute path)'],
  ['apps/desktop/src-tauri/src/rpc.rs', 'resolves the sibling project Python distribution next to the repo (relative path only, no absolute path)'],
  ['docs/PACKAGING.md', 'documents the private path exclusions for maintainers'],
  ['README.md', 'documents the privacy/offline guarantees'],
]);

/** Files whose content is machine-generated or hash-dense: no entropy heuristic. */
const ENTROPY_SKIP = [
  /(^|\/)package-lock\.json$/,
  /(^|\/)pnpm-lock\.yaml$/,
  /(^|\/)yarn\.lock$/,
  /(^|\/)Cargo\.lock$/,
  /(^|\/)go\.sum$/,
  /\.min\.(js|css)$/,
  /\.svg$/,
  /\.(png|jpe?g|webp|gif|ico|icns)$/,
];

/** Token/credential shapes that are always suspicious. */
const SECRET_PATTERNS = [
  { id: 'aws-access-key', re: /\bAKIA[0-9A-Z]{16}\b/ },
  { id: 'github-token', re: /\bgh[pousr]_[A-Za-z0-9]{36,}\b/ },
  { id: 'github-pat', re: /\bgithub_pat_[A-Za-z0-9_]{40,}\b/ },
  { id: 'openai-key', re: /\bsk-[A-Za-z0-9_-]{20,}\b/ },
  { id: 'slack-token', re: /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/ },
  { id: 'private-key-header', re: /-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----/ },
  { id: 'google-api-key', re: /\bAIza[0-9A-Za-z_-]{35}\b/ },
];

const MAX_TEXT_BYTES = 2 * 1024 * 1024;
const MAX_SCAN_BYTES = 256 * 1024 * 1024; // per file, binary scanning
const IGNORED_DIRS = new Set([
  '.git', '.local', 'node_modules', 'target', 'dist', 'build', 'release', 'artifacts',
  '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.venv', 'venv', 'env',
  'envs', 'coverage', 'htmlcov', '.idea', '.vscode', 'tmp', 'temp',
]);

const CONSOLE = { red: '\x1b[31m', yellow: '\x1b[33m', green: '\x1b[32m', dim: '\x1b[2m', reset: '\x1b[0m', bold: '\x1b[1m' };
const color = (c, s) => (process.stdout.isTTY ? `${CONSOLE[c]}${s}${CONSOLE.reset}` : s);

// ------------------------------------------------------------------ helpers
function isBinary(buf) {
  const n = Math.min(buf.length, 8192);
  for (let i = 0; i < n; i++) if (buf[i] === 0) return true;
  return false;
}

function listRepoFiles() {
  const git = spawnSync('git', ['rev-parse', '--is-inside-work-tree'], { cwd: ROOT, encoding: 'utf8' });
  const isGit = git.status === 0 && String(git.stdout).trim() === 'true';
  if (isGit) {
    const res = spawnSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], {
      cwd: ROOT,
      encoding: 'buffer',
      maxBuffer: 64 * 1024 * 1024,
    });
    if (res.status !== 0) {
      console.warn('[leak-guard] WARN: git ls-files failed, falling back to filesystem walk');
    } else {
      const names = res.stdout.toString('utf8').split('\0').filter(Boolean);
      return { source: 'git', files: [...new Set(names)].map((f) => path.join(ROOT, f)) };
    }
  }
  const out = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.name === '.git') continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (IGNORED_DIRS.has(entry.name)) continue;
        if (entry.name.startsWith('_PRIVATE') || entry.name === 'da_eliminare') continue;
        walk(full);
      } else if (entry.isFile()) {
        out.push(full);
      }
    }
  };
  walk(ROOT);
  return { source: 'filesystem', files: out };
}

function readCapped(file, limit) {
  const stat = fs.statSync(file);
  if (stat.size > limit) {
    const fd = fs.openSync(file, 'r');
    try {
      const buf = Buffer.alloc(limit);
      const read = fs.readSync(fd, buf, 0, limit, 0);
      return { buf: buf.subarray(0, read), size: stat.size, truncated: true };
    } finally {
      fs.closeSync(fd);
    }
  }
  return { buf: fs.readFileSync(file), size: stat.size, truncated: false };
}

function shannonEntropy(s) {
  const freq = new Map();
  for (const ch of s) freq.set(ch, (freq.get(ch) || 0) + 1);
  let h = 0;
  for (const n of freq.values()) {
    const p = n / s.length;
    h -= p * Math.log2(p);
  }
  return h;
}

function scanTextForSecrets(file, buf) {
  const findings = [];
  const text = buf.toString('utf8');
  for (const pat of SECRET_PATTERNS) {
    const m = text.match(pat.re);
    if (m) findings.push({ kind: 'secret', rule: pat.id, sample: `${m[0].slice(0, 12)}…` });
  }
  if (!ENTROPY_SKIP.some((re) => re.test(rel(file)))) {
    // Generic high-entropy token: long base64/hex run inside an assignment-looking context.
    const lines = text.split(/\r?\n/);
    for (let i = 0; i < lines.length && findings.length < 5; i++) {
      const line = lines[i];
      const m = line.match(/(?:token|secret|passw(?:or)?d|api[_-]?key|auth|bearer|credential)[^A-Za-z0-9]{0,8}[=:]\s*['"]?([A-Za-z0-9+/_-]{32,})/i);
      if (!m) continue;
      const candidate = m[1];
      const unique = new Set(candidate).size;
      if (shannonEntropy(candidate) >= 3.6 && unique >= 12) {
        findings.push({ kind: 'secret', rule: 'high-entropy-token', line: i + 1, sample: `${candidate.slice(0, 8)}… (${candidate.length} chars)` });
      }
    }
  }
  return findings;
}

/** Markers inside arbitrary bytes (bundle verification, binary-safe). */
function scanBufferForMarkers(buf) {
  const text = buf.toString('latin1');
  const findings = [];
  for (const marker of PRIVATE_MARKERS) {
    const m = text.match(marker.re);
    if (m) findings.push({ kind: 'private-marker', rule: marker.id, hint: marker.hint, sample: m[0].slice(0, 60) });
  }
  return findings;
}

// ------------------------------------------------------------------- modes
function scanRepo() {
  const { source, files } = listRepoFiles();
  if (files.length === 0) {
    console.log('[leak-guard] no files to scan');
    return 0;
  }
  console.log(`[leak-guard] repo root: ${ROOT}`);
  console.log(`[leak-guard] source: ${source} (${files.length} files)`);

  const violations = [];
  const warnings = [];
  const allowlisted = [];

  for (const file of files) {
    const relative = rel(file);
    let stat;
    try {
      stat = fs.statSync(file);
    } catch {
      continue;
    }
    if (!stat.isFile()) continue;

    const ext = path.extname(relative).toLowerCase();
    if (FORBIDDEN_EXTENSIONS.has(ext)) {
      violations.push({ file: relative, findings: [{ kind: 'forbidden-extension', rule: ext, hint: 'must not be tracked in the public repository' }] });
      continue;
    }
    if (stat.size > MAX_SCAN_BYTES) {
      warnings.push({ file: relative, message: `skipped: ${stat.size} bytes > ${MAX_SCAN_BYTES}` });
      continue;
    }
    const { buf } = readCapped(file, MAX_SCAN_BYTES);
    if (isBinary(buf)) {
      // Binary files: still check marker strings (paths can leak inside binaries).
      const markerFindings = scanBufferForMarkers(buf);
      if (markerFindings.length) violations.push({ file: relative, findings: markerFindings });
      continue;
    }
    const allowReason = FILE_ALLOWLIST.get(relative);
    const privateFindings = allowReason ? [] : scanBufferForMarkers(buf);
    if (allowReason && scanBufferForMarkers(buf).length) {
      allowlisted.push({ file: relative, reason: allowReason });
    }
    const secretFindings = buf.length <= MAX_TEXT_BYTES ? scanTextForSecrets(file, buf) : [];
    const findings = [...privateFindings, ...secretFindings];
    if (findings.length) violations.push({ file: relative, findings });
  }

  return report(violations, warnings, allowlisted);
}

function scanPath(target) {
  const abs = path.resolve(target);
  if (!fs.existsSync(abs)) {
    console.error(`[leak-guard] ERROR: path not found: ${abs}`);
    return 2;
  }
  const violations = [];
  const warnings = [];
  const fileList = [];
  const collect = (p) => {
    const stat = fs.statSync(p);
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(p, { withFileTypes: true })) collect(path.join(p, entry.name));
    } else if (stat.isFile()) {
      fileList.push(p);
    }
  };
  collect(abs);
  console.log(`[leak-guard] scanning ${fileList.length} files under ${abs}`);

  for (const file of fileList) {
    const display = path.relative(ROOT, file) || file;
    const ext = path.extname(file).toLowerCase();
    if (FORBIDDEN_EXTENSIONS.has(ext)) {
      if (['.exe', '.msi', '.dmg', '.appimage', '.deb', '.rpm', '.dll', '.so', '.dylib', '.app'].includes(ext)) {
        // Shipped executable artifacts are expected in dist/: only their embedded strings matter.
      } else {
        violations.push({ file: display, findings: [{ kind: 'forbidden-extension', rule: ext, hint: 'private media/key/model inside a distributable' }] });
        continue;
      }
    }
    const stat = fs.statSync(file);
    if (stat.size > MAX_SCAN_BYTES) {
      warnings.push({ file: display, message: `skipped: ${stat.size} bytes > ${MAX_SCAN_BYTES}` });
      continue;
    }
    const { buf } = readCapped(file, MAX_SCAN_BYTES);
    const findings = scanBufferForMarkers(buf);
    if (findings.length) violations.push({ file: display, findings });
  }
  return report(violations, warnings, []);
}

function report(violations, warnings, allowlisted) {
  for (const w of warnings) console.log(color('yellow', `  [warn] ${w.file}: ${w.message}`));
  for (const a of allowlisted) console.log(color('dim', `  [allow] ${a.file} — ${a.reason}`));
  if (violations.length === 0) {
    console.log(color('green', '[leak-guard] OK - no violations found'));
    return 0;
  }
  console.error('');
  console.error(color('red', `[leak-guard] FAIL — ${violations.length} file(s) with violations:`));
  for (const v of violations) {
    console.error(color('red', `  ${v.file}`));
    for (const f of v.findings) {
      const where = f.line ? `:${f.line}` : '';
      const sample = f.sample ? ` sample="${f.sample}"` : '';
      console.error(`    - [${f.kind}/${f.rule}]${where}${f.hint ? ` ${f.hint}` : ''}${sample}`);
    }
  }
  console.error('');
  console.error('[leak-guard] fix or relocate these files before committing/packaging.');
  return 1;
}

function selfTest() {
  const tmpRoot = path.join(ROOT, '.local', 'tmp', 'leak-guard-selftest');
  fs.rmSync(tmpRoot, { recursive: true, force: true });
  fs.mkdirSync(tmpRoot, { recursive: true });
  // Both a private path and a credential-looking token, assembled so this script
  // does not itself contain a literal violation that a scanner would flag.
  const drive = 'K' + ':' + '\\' + 'AI' + '\\' + 'VoiceShield_2026';
  const tokenName = ['api', 'key'].join('_');
  // Assembled at runtime on purpose: no literal credential shape lives in this file.
  const fakeToken = ['A1b2C3d4E5f6G7h8', 'I9j0K1l2M3n4O5p6', 'Q7r8'].join('');
  const probe = path.join(tmpRoot, 'violation.txt');
  fs.writeFileSync(probe, `${drive}\\secret.wav\n${tokenName}: ${fakeToken}\n_PRIVATE_RESEARCH_LAB/notes.md\n`);
  let findings = 0;
  try {
    const { buf } = readCapped(probe, MAX_SCAN_BYTES);
    findings += scanBufferForMarkers(buf).length;
    findings += scanTextForSecrets(probe, buf).length;
  } finally {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
  const removed = !fs.existsSync(tmpRoot);
  if (findings >= 3 && removed) {
    console.log(color('green', `[leak-guard] self-test PASS — ${findings} violations detected in a temporary file, cleaned up`));
    return 0;
  }
  console.error(color('red', `[leak-guard] self-test FAIL — findings=${findings}, cleanedUp=${removed}`));
  return 1;
}

function main() {
  const args = process.argv.slice(2);
  if (args.includes('--help') || args.includes('-h')) {
    console.log('Usage: node tooling/leak-guard.mjs [--all | --scan <path> | --self-test | --list]');
    return 0;
  }
  if (args.includes('--self-test')) return selfTest();
  if (args.includes('--list')) {
    const { source, files } = listRepoFiles();
    console.log(`[leak-guard] source: ${source} (${files.length} files)`);
    for (const f of files) console.log(rel(f));
    return 0;
  }
  const scanIndex = args.indexOf('--scan');
  if (scanIndex !== -1) {
    const target = args[scanIndex + 1];
    if (!target) {
      console.error('[leak-guard] ERROR: --scan requires a path');
      return 2;
    }
    return scanPath(target);
  }
  return scanRepo();
}

process.exit(main());
