//! Risoluzione di percorsi e dell'entrypoint del motore Python.
//!
//! L'interprete di progetto è compilato **isolato** (`sys.flags.isolated = 1`):
//! `PYTHONPATH` e `-m` non funzionano. L'entrypoint ufficiale è quindi un
//! launcher esplicito:
//!
//! ```text
//! <python> <repo>/packages/engine/nlvs_rpc.py --data-dir <dir>
//! ```
//!
//! Ordine di risoluzione dell'entrypoint:
//!   1. `NLVS_ENGINE_BIN` (eseguibile frozen del sidecar) — override esplicito
//!   2. `NLVS_ENGINE_PYTHON` + launcher `nlvs_rpc.py` (sviluppo/test)
//!   3. bundle: `resource_dir/engine/frozen/.../nlvo-engine[.exe]`
//!   4. bundle: `resource_dir/engine/runtime/python*` + `nlvs_rpc.py` nel bundle
//!   5. dev: distribuzione Python embedded in una directory sorella del repo
//!      (nessun nome di cartella hardcoded) + `packages/engine/nlvs_rpc.py`
//!   6. dev: `.local/runtime/<os>-<arch>/python*`
//!   7. errore esplicito — MAI il PATH di sistema.
//!
//! Fallback documentato: se il launcher non esiste ma l'interprete non è isolato,
//! si usa `-m nl_vo_shield.rpc`. Nessun percorso assoluto è codificato qui.

use crate::error::{NlError, NlResult};
use crate::hostconfig::HostConfig;
use serde::Serialize;
use serde_json::json;
use std::path::{Path, PathBuf};
use tauri::{AppHandle, Manager};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PythonSource {
    Env,
    Bundled,
    DevProject,
    LocalRuntime,
    Frozen,
    Missing,
}

impl PythonSource {
    pub fn label(self) -> &'static str {
        match self {
            PythonSource::Env => "NLVS_ENGINE_PYTHON",
            PythonSource::Bundled => "bundle engine/runtime",
            PythonSource::DevProject => "dev (python embedded sorella)",
            PythonSource::LocalRuntime => ".local/runtime",
            PythonSource::Frozen => "sidecar frozen",
            PythonSource::Missing => "non trovato",
        }
    }
}

/// Solo diagnostica interna/test: descrizione leggibile della provenienza.
#[allow(dead_code)]
pub fn source_label(source: PythonSource) -> &'static str {
    source.label()
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PythonResolution {
    pub found: bool,
    pub path: Option<String>,
    pub source: PythonSource,
    pub module: String,
    /// `script` (launcher nlvs_rpc.py), `frozen` (binario) o `module` (fallback -m).
    pub mode: String,
    pub launcher: Option<String>,
    pub tried: Vec<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HostPaths {
    pub data_dir: String,
    pub models_dir: String,
    pub cache_dir: String,
    pub output_dir: String,
    pub config_dir: String,
    pub settings_file: String,
    pub install_dir: String,
    pub install_writable: bool,
    /// true quando non esiste ancora una cartella dati scrivibile: la UI deve chiederla.
    pub needs_data_dir: bool,
    pub suggested_data_dir: String,
    pub portable: bool,
    pub portable_available: bool,
    pub portable_reason: Option<String>,
    /// `install` (dentro l'installazione), `user` (scelta dall'utente), `unresolved`.
    pub data_dir_source: String,
}

/// Sottocartelle standard della cartella dati (tutto dentro l'installazione o dove scelto).
pub const DATA_SUBDIRS: [&str; 7] = [
    "models",
    "cache",
    "certs",
    "receipts",
    "profiles",
    "logs",
    "output",
];

fn ensure_data_structure(data_dir: &Path) -> NlResult<()> {
    std::fs::create_dir_all(data_dir).map_err(|error| {
        NlError::new(
            "DATA_DIR_NOT_WRITABLE",
            format!("Impossibile creare la cartella dati {}: {error}", data_dir.display()),
        )
    })?;
    for name in DATA_SUBDIRS {
        let dir = data_dir.join(name);
        std::fs::create_dir_all(&dir).map_err(|error| {
            NlError::new(
                "DATA_DIR_NOT_WRITABLE",
                format!("Impossibile creare {}: {error}", dir.display()),
            )
        })?;
    }
    Ok(())
}

/// Percorso del file di preferenze (puntatore alla cartella dati e opzioni UI).
/// Prima scelta: `<install>/data/settings.json`; se l'installazione non è scrivibile,
/// `%LOCALAPPDATA%/NL-VO-Shield/settings.json` — SOLO per il puntatore, mai i dati.
/// Non viene mai usata `%APPDATA%` (Roaming) né una data dir globale.
pub fn resolve_config_path(install_dir: &Path) -> PathBuf {
    let install_data = install_dir.join("data");
    if is_dir_writable(&install_data) {
        return install_data.join("settings.json");
    }
    if let Ok(local_app_data) = std::env::var("LOCALAPPDATA") {
        let dir = PathBuf::from(local_app_data).join("NL-VO-Shield");
        if std::fs::create_dir_all(&dir).is_ok() {
            return dir.join("settings.json");
        }
    }
    install_data.join("settings.json")
}

/// Come avviare il processo del motore.
#[derive(Debug, Clone)]
pub enum EngineEntry {
    /// `<python> <script> --data-dir …` (sviluppo o bundle con runtime).
    Script { python: PathBuf, script: PathBuf },
    /// `<python> -m <module> --data-dir …` (fallback per interpreti non isolati).
    Module { python: PathBuf, module: String },
    /// Binario frozen del sidecar.
    Frozen { exe: PathBuf },
}

#[derive(Debug, Clone)]
pub struct EngineLaunch {
    pub entry: EngineEntry,
    pub source: PythonSource,
    pub cwd: PathBuf,
    pub data_dir: PathBuf,
    pub models_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub project_root: Option<PathBuf>,
    pub module_roots: Vec<PathBuf>,
    /// Binario c2patool incluso nel pacchetto (passato come NLVS_C2PATOOL).
    pub c2patool: Option<PathBuf>,
}

impl EngineLaunch {
    /// Firma stabile per decidere se il motore va riavviato.
    pub fn version_token(&self) -> String {
        let entry = match &self.entry {
            EngineEntry::Script { python, script } => {
                format!("script|{}|{}", python.display(), script.display())
            }
            EngineEntry::Module { python, module } => {
                format!("module|{}|{}", python.display(), module)
            }
            EngineEntry::Frozen { exe } => format!("frozen|{}", exe.display()),
        };
        format!(
            "{}|{}|{}|{}",
            entry,
            self.data_dir.display(),
            self.models_dir.display(),
            self.cache_dir.display()
        )
    }
}

pub fn exe_dir() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
}

/// Scopre la radice del repository pubblico senza percorsi assoluti.
pub fn find_repo_root() -> Option<PathBuf> {
    if let Ok(value) = std::env::var("NLVS_PROJECT_ROOT") {
        let candidate = PathBuf::from(value.trim());
        if candidate.is_dir() {
            return Some(candidate);
        }
    }

    let markers = ["packages/engine", "apps/desktop"];
    let mut anchors: Vec<PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        anchors.push(cwd);
    }
    if let Some(dir) = exe_dir() {
        anchors.push(dir);
    }

    for anchor in anchors {
        for candidate in anchor.ancestors() {
            if markers.iter().all(|marker| candidate.join(marker).is_dir()) {
                return Some(candidate.to_path_buf());
            }
        }
    }
    None
}

pub fn os_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    }
}

pub fn arch_name() -> &'static str {
    if cfg!(target_arch = "x86_64") {
        "x86_64"
    } else if cfg!(target_arch = "aarch64") {
        "aarch64"
    } else if cfg!(target_arch = "x86") {
        "x86"
    } else {
        "unknown"
    }
}

fn python_names_windows() -> [&'static str; 2] {
    ["python.exe", "python3.exe"]
}

fn python_candidates(dir: &Path) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if cfg!(windows) {
        for name in python_names_windows() {
            candidates.push(dir.join(name));
        }
    }
    for name in ["bin/python3", "bin/python", "python3", "python"] {
        candidates.push(dir.join(name));
    }
    candidates.into_iter().filter(|path| path.is_file()).collect()
}

fn first_python_in(dir: &Path) -> Option<PathBuf> {
    python_candidates(dir).into_iter().next()
}

/// Riconosce una distribuzione Python embedded (nessun nome hardcoded).
fn embedded_python_in(dir: &Path) -> Option<PathBuf> {
    let looks_like_distribution = dir.join("Lib").is_dir()
        || dir.join("lib").is_dir()
        || dir.join("pyvenv.cfg").is_file()
        || std::fs::read_dir(dir)
            .map(|entries| {
                entries.flatten().any(|entry| {
                    let name = entry.file_name().to_string_lossy().to_lowercase();
                    name.starts_with("python") && name.ends_with(".zip")
                })
            })
            .unwrap_or(false);
    if !looks_like_distribution {
        return None;
    }
    first_python_in(dir)
}

/// Distribuzione Python embedded in una directory sorella del repo (dev root).
pub fn find_sibling_python(project_root: &Path) -> Option<PathBuf> {
    let parent = project_root.parent()?;
    let entries = std::fs::read_dir(parent).ok()?;
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_dir() && path.as_path() != project_root)
        .collect();
    dirs.sort();
    for dir in dirs {
        if let Some(found) = embedded_python_in(&dir) {
            return Some(found);
        }
    }
    None
}

/// Runtime Python sotto `.local/runtime` (layout python-build-standalone).
pub fn find_local_runtime_python(repo: &Path) -> Option<PathBuf> {
    let runtime_root = repo.join(".local").join("runtime");
    let preferred = runtime_root.join(format!("{}-{}", os_name(), arch_name()));
    if let Some(found) = find_python_under(&preferred) {
        return Some(found);
    }
    find_python_under(&runtime_root)
}

/// Interprete di sviluppo: sibling del repo, poi `.local/runtime`.
#[allow(dead_code)]
pub fn find_dev_python(repo: &Path) -> Option<PathBuf> {
    find_sibling_python(repo).or_else(|| find_local_runtime_python(repo))
}

/// Cerca un interprete fino a due livelli sotto `root`.
fn find_python_under(root: &Path) -> Option<PathBuf> {
    if let Some(found) = first_python_in(root) {
        return Some(found);
    }
    let Ok(entries) = std::fs::read_dir(root) else {
        return None;
    };
    let mut dirs: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    dirs.sort();
    for dir in dirs {
        if let Some(found) = first_python_in(&dir) {
            return Some(found);
        }
        if let Ok(children) = std::fs::read_dir(&dir) {
            let mut child_dirs: Vec<PathBuf> = children
                .flatten()
                .map(|entry| entry.path())
                .filter(|path| path.is_dir())
                .collect();
            child_dirs.sort();
            for child in child_dirs {
                if let Some(found) = first_python_in(&child) {
                    return Some(found);
                }
            }
        }
    }
    None
}

pub fn engine_module() -> String {
    std::env::var("NLVS_ENGINE_MODULE")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "nl_vo_shield.rpc".to_string())
}

fn launcher_name() -> &'static str {
    "nlvs_rpc.py"
}

fn frozen_name() -> &'static str {
    if cfg!(windows) {
        "nlvo-engine.exe"
    } else {
        "nlvo-engine"
    }
}

/// Candidati per il launcher `nlvs_rpc.py`, in ordine di preferenza.
fn launcher_candidates(app: &AppHandle, repo: Option<&Path>) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(value) = std::env::var("NLVS_ENGINE_LAUNCHER") {
        let path = PathBuf::from(value.trim());
        if !value.trim().is_empty() {
            candidates.push(path);
        }
    }
    if let Some(repo) = repo {
        candidates.push(repo.join("packages").join("engine").join(launcher_name()));
    }
    if let Ok(resource) = app.path().resource_dir() {
        for base in [
            resource.join("engine"),
            resource.join("engine").join("app"),
            resource.join("engine").join("runtime"),
        ] {
            candidates.push(base.join(launcher_name()));
        }
    }
    if let Some(dir) = exe_dir() {
        candidates.push(dir.join(launcher_name()));
    }
    candidates
}

/// Candidati per il binario frozen del sidecar.
fn frozen_candidates(app: &AppHandle) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(value) = std::env::var("NLVS_ENGINE_BIN") {
        let path = PathBuf::from(value.trim());
        if !value.trim().is_empty() {
            candidates.push(path);
        }
    }
    if let Ok(resource) = app.path().resource_dir() {
        let engine = resource.join("engine");
        candidates.push(engine.join("frozen").join("nlvo-engine").join(frozen_name()));
        candidates.push(engine.join("frozen").join(frozen_name()));
        candidates.push(engine.join("bin").join(frozen_name()));
        candidates.push(engine.join(frozen_name()));
    }
    if let Some(dir) = exe_dir() {
        candidates.push(dir.join(frozen_name()));
    }
    candidates
}

fn first_existing(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates.iter().find(|path| path.is_file()).cloned()
}

/// Risolve l'entrypoint completo del motore.
pub fn resolve_entry(app: &AppHandle) -> NlResult<EngineLaunchEntry> {
    let repo = find_repo_root();
    let mut tried: Vec<String> = Vec::new();
    let launchers = launcher_candidates(app, repo.as_deref());
    let frozen = frozen_candidates(app);

    // 1. override esplicito del binario frozen
    if let Ok(value) = std::env::var("NLVS_ENGINE_BIN") {
        if !value.trim().is_empty() {
            let candidate = PathBuf::from(value.trim());
            if candidate.is_file() {
                return Ok(EngineLaunchEntry {
                    entry: EngineEntry::Frozen { exe: candidate },
                    source: PythonSource::Frozen,
                    tried,
                });
            }
            tried.push(format!("NLVS_ENGINE_BIN = {value} (non trovato)"));
        }
    }

    // 2. interprete esplicito da ambiente + launcher
    if let Ok(raw) = std::env::var("NLVS_ENGINE_PYTHON") {
        let value = raw.trim().to_string();
        if !value.is_empty() {
            let python = PathBuf::from(&value);
            if python.is_file() {
                if let Some(script) = first_existing(&launchers) {
                    tried.push(format!("NLVS_ENGINE_PYTHON = {value}"));
                    return Ok(EngineLaunchEntry {
                        entry: EngineEntry::Script { python, script },
                        source: PythonSource::Env,
                        tried,
                    });
                }
                tried.push(format!(
                    "NLVS_ENGINE_PYTHON = {value} (launcher {} non trovato: fallback -m)",
                    launcher_name()
                ));
                return Ok(EngineLaunchEntry {
                    entry: EngineEntry::Module {
                        python,
                        module: engine_module(),
                    },
                    source: PythonSource::Env,
                    tried,
                });
            }
            tried.push(format!("NLVS_ENGINE_PYTHON = {value} (non trovato)"));
            return Err(NlError::new(
                "ENGINE_PYTHON_INVALID",
                format!("NLVS_ENGINE_PYTHON non punta a un eseguibile esistente: {value}"),
            )
            .with_detail(json!({ "tried": tried })));
        }
    }

    // 3. bundle: binario frozen
    if let Ok(resource) = app.path().resource_dir() {
        let engine = resource.join("engine");
        let bundled_frozen: Vec<PathBuf> = frozen
            .iter()
            .filter(|path| path.starts_with(&engine))
            .cloned()
            .collect();
        if let Some(exe) = first_existing(&bundled_frozen) {
            return Ok(EngineLaunchEntry {
                entry: EngineEntry::Frozen { exe },
                source: PythonSource::Frozen,
                tried,
            });
        }
        // 4. bundle: runtime Python + launcher nel bundle
        if let Some(python) = first_python_in(&engine.join("runtime")) {
            if let Some(script) = launchers
                .iter()
                .find(|path| path.starts_with(&engine) && path.is_file())
                .cloned()
            {
                tried.push(format!("bundle runtime: {}", python.display()));
                return Ok(EngineLaunchEntry {
                    entry: EngineEntry::Script { python, script },
                    source: PythonSource::Bundled,
                    tried,
                });
            }
        }
    }

    // 5. sviluppo: python embedded sorella + launcher del repo
    if let Some(repo) = repo.as_ref() {
        if let Some(python) = find_sibling_python(repo) {
            if let Some(script) = first_existing(&launchers) {
                tried.push(format!("dev sibling: {}", python.display()));
                return Ok(EngineLaunchEntry {
                    entry: EngineEntry::Script { python, script },
                    source: PythonSource::DevProject,
                    tried,
                });
            }
            tried.push(format!(
                "dev sibling: {} (launcher {} assente)",
                python.display(),
                launcher_name()
            ));
        } else {
            tried.push("dev sibling: nessuna distribuzione Python embedded".to_string());
        }

        // 6. dev: .local/runtime
        if let Some(python) = find_local_runtime_python(repo) {
            if let Some(script) = first_existing(&launchers) {
                tried.push(format!(".local/runtime: {}", python.display()));
                return Ok(EngineLaunchEntry {
                    entry: EngineEntry::Script { python, script },
                    source: PythonSource::LocalRuntime,
                    tried,
                });
            }
        } else {
            tried.push(format!(
                ".local/runtime: {} (nessun python)",
                repo.join(".local").join("runtime").display()
            ));
        }
    } else {
        tried.push("repo root: non rilevata".to_string());
    }

    let _ = frozen;
    Err(NlError::new(
        "ENGINE_PYTHON_NOT_FOUND",
        "Nessun motore Python trovato: imposta NLVS_ENGINE_PYTHON/NLVS_ENGINE_BIN oppure usa un pacchetto con runtime incluso. Il PATH di sistema non viene mai usato.",
    )
    .with_detail(json!({ "tried": tried, "launcher": launcher_name(), "module": engine_module() })))
}

/// Esito della risoluzione: entrypoint + provenienza + tentativi.
pub struct EngineLaunchEntry {
    pub entry: EngineEntry,
    pub source: PythonSource,
    pub tried: Vec<String>,
}

/// Informazioni per la diagnostica UI (funziona anche quando il motore manca).
pub fn python_resolution(app: &AppHandle) -> PythonResolution {
    match resolve_entry(app) {
        Ok(plan) => {
            let mut resolution = PythonResolution {
                found: true,
                path: None,
                source: plan.source,
                module: engine_module(),
                mode: "script".to_string(),
                launcher: None,
                tried: plan.tried,
                error: None,
            };
            match &plan.entry {
                EngineEntry::Script { python, script } => {
                    resolution.path = Some(python.display().to_string());
                    resolution.launcher = Some(script.display().to_string());
                }
                EngineEntry::Module { python, module } => {
                    resolution.path = Some(python.display().to_string());
                    resolution.mode = "module".to_string();
                    resolution.module = module.clone();
                }
                EngineEntry::Frozen { exe } => {
                    resolution.path = Some(exe.display().to_string());
                    resolution.mode = "frozen".to_string();
                    resolution.launcher = None;
                }
            }
            resolution
        }
        Err(error) => {
            let detail = error.detail.clone().unwrap_or(serde_json::Value::Null);
            let tried: Vec<String> = detail
                .get("tried")
                .and_then(|value| value.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default();
            PythonResolution {
                found: false,
                path: None,
                source: PythonSource::Missing,
                module: engine_module(),
                mode: "none".to_string(),
                launcher: None,
                tried,
                error: Some(error.message),
            }
        }
    }
}

pub fn is_dir_writable(dir: &Path) -> bool {
    if std::fs::create_dir_all(dir).is_err() {
        return false;
    }
    let probe = dir.join(".nlvs-write-probe");
    match std::fs::write(&probe, b"ok") {
        Ok(_) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// Percorsi effettivi. Regola di prodotto (vincolante):
/// - default: `<cartella dell'eseguibile>/data` (tutto dentro l'installazione);
/// - se la cartella non è scrivibile: `needs_data_dir = true` e la UI chiede una
///   cartella scrivibile all'utente (nessun ripiego silenzioso su AppData);
/// - modelli/cache vivono sempre dentro la cartella dati scelta.
pub fn resolve_paths(app: &AppHandle, cfg: &HostConfig) -> HostPaths {
    let _ = app;
    let install_dir = exe_dir().unwrap_or_else(|| PathBuf::from("."));
    let install_data = install_dir.join("data");
    let install_writable = is_dir_writable(&install_data);

    let mut needs_data_dir = false;
    let mut reason: Option<String> = None;
    let mut data_dir_source = "install".to_string();

    let requested = cfg
        .data_dir_override
        .as_ref()
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);

    let data_dir = match requested {
        Some(dir) => {
            data_dir_source = "user".to_string();
            if !is_dir_writable(&dir) {
                needs_data_dir = true;
                reason = Some(format!(
                    "La cartella dati scelta non è scrivibile: {}",
                    dir.display()
                ));
            }
            dir
        }
        None => {
            if install_writable {
                install_data.clone()
            } else {
                needs_data_dir = true;
                reason = Some(
                    "La cartella dell'applicazione non è scrivibile: scegli una cartella dati (suggerita accanto all'installazione o un percorso utente)."
                        .to_string(),
                );
                data_dir_source = "unresolved".to_string();
                install_data.clone()
            }
        }
    };

    let models_dir = cfg
        .models_dir_override
        .as_ref()
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.join("models"));
    let cache_dir = cfg
        .cache_dir_override
        .as_ref()
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.join("cache"));
    let output_dir = cfg
        .output_dir
        .as_ref()
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| data_dir.join("output"));

    let settings_file = resolve_config_path(&install_dir);
    let config_dir = settings_file
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| data_dir.clone());
    let portable = data_dir_source == "install";

    HostPaths {
        data_dir: data_dir.display().to_string(),
        models_dir: models_dir.display().to_string(),
        cache_dir: cache_dir.display().to_string(),
        output_dir: output_dir.display().to_string(),
        config_dir: config_dir.display().to_string(),
        settings_file: settings_file.display().to_string(),
        install_dir: install_dir.display().to_string(),
        install_writable,
        needs_data_dir,
        suggested_data_dir: install_data.display().to_string(),
        portable,
        portable_available: install_writable,
        portable_reason: reason,
        data_dir_source,
    }
}

fn existing_module_roots(app: &AppHandle, repo: Option<&Path>) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Ok(resource) = app.path().resource_dir() {
        for candidate in [
            resource.join("engine"),
            resource.join("engine").join("app"),
            resource.join("engine").join("py"),
        ] {
            if candidate.is_dir() {
                roots.push(candidate);
            }
        }
    }
    if let Some(repo) = repo {
        let engine_root = repo.join("packages").join("engine");
        if engine_root.is_dir() {
            roots.push(engine_root);
        }
    }
    roots.dedup();
    roots
}

/// Costruisce la descrizione completa del processo motore, creando la struttura dati.
pub fn build_launch(app: &AppHandle, cfg: &HostConfig) -> NlResult<EngineLaunch> {
    let plan = resolve_entry(app)?;
    let paths = resolve_paths(app, cfg);
    if paths.needs_data_dir {
        return Err(NlError::new(
            "ENGINE_DATA_DIR_UNRESOLVED",
            paths
                .portable_reason
                .clone()
                .unwrap_or_else(|| "Cartella dati non ancora scelta.".to_string()),
        )
        .with_detail(json!({
            "installDir": paths.install_dir,
            "installWritable": paths.install_writable,
            "suggestedDataDir": paths.suggested_data_dir,
            "dataDir": paths.data_dir,
            "dataDirSource": paths.data_dir_source,
        })));
    }

    let data_dir = PathBuf::from(&paths.data_dir);
    ensure_data_structure(&data_dir)?;
    let models_dir = PathBuf::from(&paths.models_dir);
    let cache_dir = PathBuf::from(&paths.cache_dir);
    for dir in [&models_dir, &cache_dir, &PathBuf::from(&paths.output_dir)] {
        std::fs::create_dir_all(dir).map_err(|error| {
            NlError::new(
                "HOST_IO_ERROR",
                format!("Impossibile creare la cartella {}: {error}", dir.display()),
            )
        })?;
    }

    let repo = find_repo_root();
    let module_roots = existing_module_roots(app, repo.as_deref());
    let cwd = module_roots
        .first()
        .cloned()
        .or_else(|| repo.clone())
        .unwrap_or_else(|| data_dir.clone());

    // c2patool incluso nel pacchetto (T3 lo aggiunge al payload engine).
    let c2patool = app.path().resource_dir().ok().and_then(|resource| {
        let names = if cfg!(windows) {
            ["c2patool.exe", "c2patool"]
        } else {
            ["c2patool", "c2patool.exe"]
        };
        for base in [resource.join("engine").join("bin").join("c2patool")] {
            for name in names {
                let candidate = base.join(name);
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
        None
    });

    Ok(EngineLaunch {
        entry: plan.entry,
        source: plan.source,
        cwd,
        data_dir,
        models_dir,
        cache_dir,
        project_root: repo,
        module_roots,
        c2patool,
    })
}
