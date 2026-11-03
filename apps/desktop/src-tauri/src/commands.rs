//! Comandi Tauri: superficie unica e tipizzata tra UI Svelte e motore Python.
//!
//! Regole:
//! - nessun percorso hardcoded (data dir da Tauri, input/output scelti dall'utente);
//! - errori sempre `{ code, message, detail }`;
//! - il motore viene preparato (launch risolto) prima di ogni chiamata RPC.

use crate::error::{NlError, NlResult};
use crate::hardware::{self, HardwareInfo};
use crate::hostconfig::HostConfig;
use crate::paths::{self, HostPaths, PythonResolution};
use crate::rpc::{timeout_for, EngineManager};
use crate::updates::{self, UpdateInfo};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use tauri::{AppHandle, Emitter, State};
use tauri_plugin_opener::OpenerExt;
use tokio::sync::Mutex;

const MAX_REPORT_BYTES: usize = 64 * 1024 * 1024;
const MAX_FILE_BYTES: u64 = 128 * 1024 * 1024;

/// Inoltro eventi motore verso la UI.
pub struct AppSink {
    pub app: AppHandle,
}

impl crate::rpc::EventSink for AppSink {
    fn emit(&self, event: &str, payload: Value) {
        let _ = self.app.emit(event, payload);
    }
}

pub struct AppCtx {
    pub app: AppHandle,
    pub engine: EngineManager,
    pub http: reqwest::Client,
    pub config: Mutex<HostConfig>,
    pub config_path: PathBuf,
    pub launch_epoch: AtomicU64,
    /// job_id del download modello in corso, per modello.
    pub model_jobs: Mutex<HashMap<String, String>>,
}

impl AppCtx {
    pub fn new(
        app: AppHandle,
        engine: EngineManager,
        config: HostConfig,
        config_path: PathBuf,
        http: reqwest::Client,
    ) -> Self {
        Self {
            app,
            engine,
            http,
            config: Mutex::new(config),
            config_path,
            launch_epoch: AtomicU64::new(0),
            model_jobs: Mutex::new(HashMap::new()),
        }
    }

    pub async fn config_snapshot(&self) -> HostConfig {
        self.config.lock().await.clone()
    }

    /// Risolve l'entrypoint con la configurazione corrente e riavvia se è cambiato.
    pub async fn prepare_engine(&self) -> NlResult<()> {
        let config = self.config_snapshot().await;
        let launch = paths::build_launch(&self.app, &config)?;
        if self.engine.set_launch(launch).await {
            self.launch_epoch.fetch_add(1, Ordering::SeqCst);
            self.engine.stop().await;
        }
        Ok(())
    }

    pub async fn host_settings_value(&self) -> Value {
        let config = self.config_snapshot().await;
        let paths = paths::resolve_paths(&self.app, &config);
        json!({
            "language": config.language_or_default(),
            "theme": config.theme_or_default(),
            "outputDir": config.output_dir,
            "openAfterJob": config.open_after_job,
            "portable": paths.portable,
            "portableAvailable": paths.portable_available,
            "portableReason": paths.portable_reason,
            "dataDir": paths.data_dir,
            "dataDirOverride": config.data_dir_override,
            "modelsDir": paths.models_dir,
            "cacheDir": paths.cache_dir,
            "autoUpdateCheck": config.auto_update_check,
        })
    }
}

async fn rpc(ctx: &AppCtx, method: &str, params: Value) -> NlResult<Value> {
    ctx.prepare_engine().await?;
    ctx.engine.request(method, params, timeout_for(method)).await
}

/* ----------------------------- engine / host ----------------------------- */

#[tauri::command]
pub async fn engine_health(state: State<'_, AppCtx>) -> NlResult<Value> {
    let ctx = state.inner();
    ctx.prepare_engine().await?;
    ctx.engine.health().await
}

#[tauri::command]
pub async fn engine_restart(state: State<'_, AppCtx>) -> NlResult<Value> {
    let ctx = state.inner();
    ctx.prepare_engine().await?;
    ctx.engine.clear_logs().await;
    ctx.engine.restart().await?;
    Ok(json!({ "ok": true }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LogsRequest {
    #[serde(default)]
    pub limit: Option<usize>,
}

#[tauri::command]
pub async fn open_engine_log(state: State<'_, AppCtx>) -> NlResult<Value> {
    let ctx = state.inner();
    let data_dir = ctx
        .config_path
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| NlError::new("LOG_DIR_MISSING", "Cartella dati non risolta."))?;
    let logs_dir = data_dir.join("logs");
    std::fs::create_dir_all(&logs_dir)?;
    let path = logs_dir.join("engine-console.log");
    let lines = ctx.engine.logs(2000).await;
    let mut content = String::new();
    content.push_str(&format!(
        "# NL VO Shield — snapshot console motore {}\n",
        chrono::Utc::now().to_rfc3339()
    ));
    for entry in &lines {
        content.push_str(&format!("{} [{}] {}\n", entry.ts, entry.level, entry.line));
    }
    std::fs::write(&path, content)?;
    let opened = ctx
        .app
        .opener()
        .open_path(path.to_string_lossy(), None::<&str>)
        .is_ok();
    Ok(json!({ "opened": opened, "path": path, "lines": lines.len() }))
}

#[tauri::command]
pub async fn engine_logs(
    state: State<'_, AppCtx>,
    request: LogsRequest,
) -> NlResult<Value> {
    let limit = request.limit.unwrap_or(250).min(2000);
    let lines = state.inner().engine.logs(limit).await;
    Ok(json!({ "lines": lines }))
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdaterStatus {
    pub auto_update_enabled: bool,
    pub release_check_available: bool,
    pub repo: String,
    pub reason: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HostInfo {
    pub app_version: String,
    pub product_name: String,
    pub identifier: String,
    pub os: String,
    pub arch: String,
    pub webview: Option<String>,
    pub paths: HostPaths,
    pub python: PythonResolution,
    pub engine_module: String,
    pub repo_root: Option<String>,
    pub language: String,
    pub theme: String,
    pub updater: UpdaterStatus,
    /// CPU/RAM/GPU reali: base per la sezione "Protezioni consigliate" (sola lettura).
    pub hardware: HardwareInfo,
}

#[tauri::command]
pub async fn host_info(state: State<'_, AppCtx>) -> NlResult<HostInfo> {
    let ctx = state.inner();
    let config = ctx.config_snapshot().await;
    let paths = paths::resolve_paths(&ctx.app, &config);
    let package = ctx.app.package_info();
    let tauri_config = ctx.app.config();
    Ok(HostInfo {
        app_version: package.version.to_string(),
        product_name: tauri_config
            .product_name
            .clone()
            .unwrap_or_else(|| "NL VO Shield".to_string()),
        identifier: tauri_config.identifier.clone(),
        os: paths::os_name().to_string(),
        arch: paths::arch_name().to_string(),
        webview: None,
        paths,
        python: paths::python_resolution(&ctx.app),
        engine_module: paths::engine_module(),
        repo_root: paths::find_repo_root().map(|path| path.display().to_string()),
        language: config.language_or_default(),
        theme: config.theme_or_default(),
        updater: UpdaterStatus {
            auto_update_enabled: false,
            release_check_available: true,
            repo: updates::REPO.to_string(),
            reason: Some(
                "Firma degli aggiornamenti non configurata: auto-update disabilitato, controllo manuale della release attivo."
                    .to_string(),
            ),
        },
        hardware: hardware::detect().clone(),
    })
}

/* -------------------------------- engines -------------------------------- */

#[tauri::command]
pub async fn list_engines(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "engines.list", json!({})).await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InstallHintRequest {
    pub engine_id: String,
}

#[tauri::command]
pub async fn engine_install_hint(
    state: State<'_, AppCtx>,
    request: InstallHintRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "engines.install_hint",
        json!({ "engine_id": request.engine_id }),
    )
    .await
}

/* ---------------------------------- jobs --------------------------------- */

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StartJobRequest {
    pub input_path: String,
    pub output_dir: String,
    #[serde(default)]
    pub chain: Vec<Value>,
    #[serde(default)]
    pub options: Option<Value>,
    #[serde(default)]
    pub preset: Option<String>,
}

#[tauri::command]
pub async fn start_job(
    state: State<'_, AppCtx>,
    request: StartJobRequest,
) -> NlResult<Value> {
    if request.input_path.trim().is_empty() {
        return Err(NlError::new("JOB_INPUT_MISSING", "Nessun file di input."));
    }
    if request.output_dir.trim().is_empty() {
        return Err(NlError::new(
            "JOB_OUTPUT_MISSING",
            "Nessuna cartella di output.",
        ));
    }
    rpc(
        state.inner(),
        "job.start",
        json!({
            "input_path": request.input_path,
            "output_dir": request.output_dir,
            "chain": request.chain,
            "options": request.options.unwrap_or_else(|| json!({})),
            "preset": request.preset,
        }),
    )
    .await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct JobIdRequest {
    pub job_id: String,
}

#[tauri::command]
pub async fn cancel_job(state: State<'_, AppCtx>, request: JobIdRequest) -> NlResult<Value> {
    rpc(
        state.inner(),
        "job.cancel",
        json!({ "job_id": request.job_id }),
    )
    .await
}

#[tauri::command]
pub async fn job_status(state: State<'_, AppCtx>, request: JobIdRequest) -> NlResult<Value> {
    rpc(
        state.inner(),
        "job.status",
        json!({ "job_id": request.job_id }),
    )
    .await
}

#[tauri::command]
pub async fn job_list(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "job.list", json!({})).await
}

/* -------------------------------- verify --------------------------------- */

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VerifyFileRequest {
    pub path: String,
    #[serde(default)]
    pub receipts_dir: Option<String>,
}

#[tauri::command]
pub async fn verify_file(
    state: State<'_, AppCtx>,
    request: VerifyFileRequest,
) -> NlResult<Value> {
    let mut params = json!({ "path": request.path });
    if let Some(receipts_dir) = request.receipts_dir.filter(|value| !value.trim().is_empty()) {
        params["receipts_dir"] = json!(receipts_dir);
    }
    rpc(state.inner(), "verify.run", params).await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct QualityRequest {
    pub path: String,
    #[serde(default)]
    pub reference_path: Option<String>,
}

#[tauri::command]
pub async fn analyze_quality(
    state: State<'_, AppCtx>,
    request: QualityRequest,
) -> NlResult<Value> {
    let mut params = json!({ "path": request.path });
    if let Some(reference_path) = request
        .reference_path
        .filter(|value| !value.trim().is_empty())
    {
        params["reference_path"] = json!(reference_path);
    }
    rpc(state.inner(), "quality.analyze", params).await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StartBatchRequest {
    pub inputs: Vec<String>,
    pub output_dir: String,
    #[serde(default)]
    pub chain: Vec<Value>,
    #[serde(default)]
    pub options: Option<Value>,
    #[serde(default)]
    pub preset: Option<String>,
}

#[tauri::command]
pub async fn start_batch(
    state: State<'_, AppCtx>,
    request: StartBatchRequest,
) -> NlResult<Value> {
    if request.inputs.is_empty() {
        return Err(NlError::new(
            "BATCH_EMPTY",
            "Nessun file nella coda del batch.",
        ));
    }
    rpc(
        state.inner(),
        "batch.start",
        json!({
            "inputs": request.inputs,
            "output_dir": request.output_dir,
            "chain": request.chain,
            "options": request.options.unwrap_or_else(|| json!({})),
            "preset": request.preset,
        }),
    )
    .await
}

#[tauri::command]
pub async fn run_doctor(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "doctor.run", json!({})).await
}

/* ---------------------------- profiles / presets ------------------------- */

#[tauri::command]
pub async fn list_profiles(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "profiles.list", json!({})).await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProfileRequest {
    pub profile: Value,
}

#[tauri::command]
pub async fn save_profile(
    state: State<'_, AppCtx>,
    request: ProfileRequest,
) -> NlResult<Value> {
    if !request.profile.is_object() {
        return Err(NlError::new(
            "PROFILE_INVALID",
            "Il profilo deve essere un oggetto JSON.",
        ));
    }
    rpc(
        state.inner(),
        "profiles.save",
        json!({ "profile": request.profile }),
    )
    .await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct IdRequest {
    pub id: String,
}

#[tauri::command]
pub async fn delete_profile(state: State<'_, AppCtx>, request: IdRequest) -> NlResult<Value> {
    rpc(
        state.inner(),
        "profiles.delete",
        json!({ "id": request.id }),
    )
    .await
}

#[tauri::command]
pub async fn list_presets(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "presets.list", json!({})).await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PresetRequest {
    pub preset: Value,
}

#[tauri::command]
pub async fn save_preset(state: State<'_, AppCtx>, request: PresetRequest) -> NlResult<Value> {
    if !request.preset.is_object() {
        return Err(NlError::new(
            "PRESET_INVALID",
            "Il preset deve essere un oggetto JSON.",
        ));
    }
    rpc(
        state.inner(),
        "presets.save",
        json!({ "preset": request.preset }),
    )
    .await
}

#[tauri::command]
pub async fn list_receipts(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "receipts.list", json!({})).await
}

#[tauri::command]
pub async fn verify_receipt(state: State<'_, AppCtx>, request: IdRequest) -> NlResult<Value> {
    rpc(
        state.inner(),
        "receipts.verify",
        json!({ "id": request.id }),
    )
    .await
}

/* -------------------------------- settings ------------------------------- */

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SetSettingsRequest {
    #[serde(default)]
    pub settings: Option<Value>,
    #[serde(default)]
    pub host: Option<Value>,
}

async fn settings_payload(ctx: &AppCtx, engine_error: Option<NlError>) -> NlResult<Value> {
    let mut engine_settings = json!({});
    let mut error = engine_error;
    if error.is_none() && ctx.engine.is_running().await {
        match ctx
            .engine
            .request("settings.get", json!({}), timeout_for("settings.get"))
            .await
        {
            Ok(value) => {
                engine_settings = value.get("settings").cloned().unwrap_or(value);
            }
            Err(err) => error = Some(err),
        }
    }
    Ok(json!({
        "settings": engine_settings,
        "host": ctx.host_settings_value().await,
        "engineError": error,
    }))
}

#[tauri::command]
pub async fn get_settings(state: State<'_, AppCtx>) -> NlResult<Value> {
    settings_payload(state.inner(), None).await
}

async fn apply_host_settings(ctx: &AppCtx, host: Option<Value>) -> NlResult<bool> {
    let Some(host) = host else {
        return Ok(false);
    };
    let Some(map) = host.as_object() else {
        return Ok(false);
    };
    let before = {
        let config = ctx.config.lock().await;
        paths::resolve_paths(&ctx.app, &config)
    };
    {
        let mut config = ctx.config.lock().await;
        if let Some(value) = map.get("language").and_then(Value::as_str) {
            if value == "it" || value == "en" {
                config.language = Some(value.to_string());
            }
        }
        if let Some(value) = map.get("theme").and_then(Value::as_str) {
            if value == "dark" || value == "light" {
                config.theme = Some(value.to_string());
            }
        }
        if map.contains_key("outputDir") {
            config.output_dir = map
                .get("outputDir")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string);
        }
        if let Some(value) = map.get("openAfterJob").and_then(Value::as_bool) {
            config.open_after_job = value;
        }
        if let Some(value) = map.get("autoUpdateCheck").and_then(Value::as_bool) {
            config.auto_update_check = value;
        }
        if map.contains_key("dataDir") {
            config.data_dir_override = map
                .get("dataDir")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string);
        }        if map.contains_key("modelsDir") {
            config.models_dir_override = map
                .get("modelsDir")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string);
        }
        if map.contains_key("cacheDir") {
            config.cache_dir_override = map
                .get("cacheDir")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string);
        }
        if let Some(value) = map.get("portable").and_then(Value::as_bool) {
            if value {
                let available = paths::exe_dir()
                    .map(|dir| paths::is_dir_writable(&dir))
                    .unwrap_or(false);
                if !available {
                    return Err(NlError::new(
                        "PORTABLE_UNAVAILABLE",
                        "La cartella dell'eseguibile non è scrivibile: modalità portable non disponibile.",
                    ));
                }
            }
            config.portable = Some(value);
        }
        if let Some(value) = map.get("dataDir").and_then(Value::as_str) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                let dir = PathBuf::from(trimmed);
                if !paths::is_dir_writable(&dir) {
                    return Err(NlError::new(
                        "DATA_DIR_NOT_WRITABLE",
                        format!("La cartella dati scelta non è scrivibile: {trimmed}"),
                    )
                    .with_detail(json!({ "path": trimmed })));
                }
            }
        }
        config.save(&ctx.config_path)?;
    }
    let after = {
        let config = ctx.config.lock().await;
        paths::resolve_paths(&ctx.app, &config)
    };
    Ok(before.data_dir != after.data_dir
        || before.models_dir != after.models_dir
        || before.cache_dir != after.cache_dir)
}

#[tauri::command]
pub async fn set_settings(
    state: State<'_, AppCtx>,
    request: SetSettingsRequest,
) -> NlResult<Value> {
    let ctx = state.inner();
    let paths_changed = apply_host_settings(ctx, request.host).await?;
    if paths_changed {
        ctx.engine.stop().await;
    }

    let mut engine_error: Option<NlError> = None;
    if let Some(settings) = request.settings {
        let is_object = settings
            .as_object()
            .map(|map| !map.is_empty())
            .unwrap_or(false);
        if is_object {
            match rpc(ctx, "settings.set", json!({ "settings": settings })).await {
                Ok(_) => {}
                Err(error) => engine_error = Some(error),
            }
        }
    }
    settings_payload(ctx, engine_error).await
}

/* --------------------------- filesystem / report ------------------------- */

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OpenPathRequest {
    pub path: String,
    #[serde(default)]
    pub kind: Option<String>,
}

#[tauri::command]
pub async fn open_path(state: State<'_, AppCtx>, request: OpenPathRequest) -> NlResult<Value> {
    let ctx = state.inner();
    let path = request.path.trim();
    if path.is_empty() {
        return Err(NlError::new("OPEN_PATH_EMPTY", "Percorso vuoto."));
    }
    if path.starts_with("http://") || path.starts_with("https://") || path.starts_with("mailto:") {
        ctx.app
            .opener()
            .open_url(path, None::<&str>)
            .map_err(|error| NlError::new("OPEN_PATH_FAILED", error.to_string()))?;
        return Ok(json!({ "opened": true }));
    }
    let target = PathBuf::from(path);
    if !target.exists() {
        return Err(NlError::new(
            "OPEN_PATH_NOT_FOUND",
            format!("Il percorso non esiste: {path}"),
        ));
    }
    let kind = request.kind.as_deref().unwrap_or("file");
    if kind == "folder" && !target.is_dir() {
        return Err(NlError::new(
            "OPEN_PATH_KIND_MISMATCH",
            format!("Il percorso non è una cartella: {path}"),
        ));
    }
    if kind == "file" && target.is_dir() {
        return Err(NlError::new(
            "OPEN_PATH_KIND_MISMATCH",
            format!("Il percorso è una cartella: {path}"),
        ));
    }
    ctx.app
        .opener()
        .open_path(path, None::<&str>)
        .map_err(|error| NlError::new("OPEN_PATH_FAILED", error.to_string()))?;
    Ok(json!({ "opened": true }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExportReportRequest {
    pub content: String,
    #[serde(default)]
    pub file_name: Option<String>,
    #[serde(default)]
    pub path: Option<String>,
}

fn safe_file_name(name: &str, fallback: &str) -> String {
    let base = name
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(name)
        .trim()
        .to_string();
    let filtered: String = base
        .chars()
        .filter(|character| {
            !matches!(
                character,
                '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' | '\0'
            )
        })
        .collect();
    if filtered.is_empty() {
        fallback.to_string()
    } else {
        filtered
    }
}

#[tauri::command]
pub async fn export_report(
    state: State<'_, AppCtx>,
    request: ExportReportRequest,
) -> NlResult<Value> {
    let ctx = state.inner();
    if request.content.len() > MAX_REPORT_BYTES {
        return Err(NlError::new(
            "EXPORT_TOO_LARGE",
            "Il report supera il limite di 64 MB.",
        ));
    }
    let file_name = safe_file_name(
        request.file_name.as_deref().unwrap_or("report.json"),
        "report.json",
    );
    let target = match request
        .path
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        Some(path) => PathBuf::from(path),
        None => {
            let config = ctx.config_snapshot().await;
            let resolved = paths::resolve_paths(&ctx.app, &config);
            PathBuf::from(resolved.data_dir)
                .join("reports")
                .join(file_name)
        }
    };
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = target.with_extension("json.tmp");
    std::fs::write(&tmp, request.content.as_bytes())?;
    std::fs::rename(&tmp, &target)?;
    Ok(json!({ "saved": true, "path": target.display().to_string() }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WaveformRequest {
    pub path: String,
    #[serde(default)]
    pub buckets: Option<usize>,
}

#[tauri::command]
pub async fn read_waveform(
    _state: State<'_, AppCtx>,
    request: WaveformRequest,
) -> NlResult<crate::waveform::WaveformData> {
    crate::waveform::read_waveform(Path::new(&request.path), request.buckets.unwrap_or(1600))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReadFileRequest {
    pub path: String,
    #[serde(default)]
    pub max_bytes: Option<u64>,
}

#[tauri::command]
pub async fn read_file_base64(
    _state: State<'_, AppCtx>,
    request: ReadFileRequest,
) -> NlResult<Value> {
    let limit = request.max_bytes.unwrap_or(MAX_FILE_BYTES).min(MAX_FILE_BYTES);
    let path = Path::new(&request.path);
    let metadata = std::fs::metadata(path)?;
    if !metadata.is_file() {
        return Err(NlError::new(
            "READ_FILE_NOT_FILE",
            "Il percorso non è un file.",
        ));
    }
    if metadata.len() > limit {
        return Err(NlError::new(
            "READ_FILE_TOO_LARGE",
            format!("File troppo grande per la lettura ({:?} byte).", metadata.len()),
        )
        .with_detail(json!({ "size": metadata.len(), "limit": limit })));
    }
    let bytes = std::fs::read(path)?;
    let encoded = base64::engine::general_purpose::STANDARD.encode(&bytes);
    Ok(json!({ "base64": encoded, "size": bytes.len() }))
}

/* --------------------------------- models -------------------------------- */

fn model_label(id: &str) -> String {
    match id {
        "audioseal-generator" => "AudioSeal — generatore watermark (16 bit)".to_string(),
        "audioseal-detector" => "AudioSeal — rilevatore watermark (16 bit)".to_string(),
        "safespeech-weights" => "SafeSpeech — checkpoint di ricerca (utente)".to_string(),
        other => other.to_string(),
    }
}

/// Converte il modello del motore (snake_case) nel formato camelCase della UI.
/// Il registro modelli e i download vivono nel core Python: l'host non duplica
/// catalogo, checksum o percorsi, evita divergenze tra CLI e desktop.
fn map_engine_models(value: &Value) -> Vec<Value> {
    let Some(items) = value.as_array() else {
        return Vec::new();
    };
    items
        .iter()
        .map(|item| {
            let get = |key: &str| item.get(key).cloned().unwrap_or(Value::Null);
            let id = item.get("id").and_then(Value::as_str).unwrap_or_default();
            json!({
                "id": id,
                "label": model_label(id),
                "engineId": get("engine"),
                "kind": get("kind"),
                "present": item.get("present").and_then(Value::as_bool).unwrap_or(false),
                "path": get("path"),
                "sizeBytes": item.get("size_bytes").cloned().unwrap_or(json!(0)),
                "expectedSizeBytes": get("expected_size_bytes"),
                "sha256": get("sha256"),
                "expectedSha256": get("expected_sha256"),
                "integrity": get("integrity"),
                "source": get("source"),
                "licenseNote": get("license_note"),
                "reason": get("reason"),
                "details": get("details"),
                "downloadAllowed": item
                    .get("download_allowed")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                "requiresDownload": item
                    .get("requires_download")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                "requiredFor": get("required_for"),
                "externalFound": get("external_found"),
                "externalMissing": get("external_missing"),
            })
        })
        .collect()
}

#[tauri::command]
pub async fn list_models(state: State<'_, AppCtx>) -> NlResult<Value> {
    let ctx = state.inner();
    let payload = rpc(ctx, "models.list", json!({ "verify_hash": false })).await?;
    let models = map_engine_models(payload.get("models").unwrap_or(&Value::Null));
    Ok(json!({
        "models": models,
        "dirs": {
            "modelsDir": payload.get("models_dir").cloned().unwrap_or(Value::Null),
            "cacheDir": payload.get("cache_dir").cloned().unwrap_or(Value::Null),
        },
        "totalBytes": payload.get("total_bytes").cloned().unwrap_or(json!(0)),
    }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DownloadModelRequest {
    pub model_id: String,
}

#[tauri::command]
pub async fn download_model(
    state: State<'_, AppCtx>,
    request: DownloadModelRequest,
) -> NlResult<Value> {
    let ctx = state.inner();
    let response = rpc(
        ctx,
        "models.download",
        json!({ "id": request.model_id, "confirmed": true }),
    )
    .await?;
    let job_id = response
        .get("job_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if !job_id.is_empty() {
        ctx.model_jobs
            .lock()
            .await
            .insert(request.model_id.clone(), job_id.clone());
    }
    Ok(json!({
        "modelId": request.model_id,
        "jobId": job_id,
        "started": !job_id.is_empty(),
    }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CancelModelDownloadRequest {
    pub model_id: String,
    #[serde(default)]
    pub job_id: Option<String>,
}

#[tauri::command]
pub async fn cancel_model_download(
    state: State<'_, AppCtx>,
    request: CancelModelDownloadRequest,
) -> NlResult<Value> {
    let ctx = state.inner();
    let job_id = match request.job_id.filter(|value| !value.trim().is_empty()) {
        Some(job_id) => job_id,
        None => ctx
            .model_jobs
            .lock()
            .await
            .get(&request.model_id)
            .cloned()
            .unwrap_or_default(),
    };
    if job_id.is_empty() {
        return Ok(json!({ "cancelled": false, "reason": "Nessun download in corso" }));
    }
    let response = rpc(ctx, "job.cancel", json!({ "job_id": job_id })).await?;
    let cancelled = response
        .get("cancelled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if cancelled {
        ctx.model_jobs.lock().await.remove(&request.model_id);
    }
    Ok(json!({ "cancelled": cancelled }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DeleteModelsRequest {
    pub model_ids: Vec<String>,
}

#[tauri::command]
pub async fn delete_model(
    state: State<'_, AppCtx>,
    request: DeleteModelsRequest,
) -> NlResult<Value> {
    let ctx = state.inner();
    let list_total = |payload: Option<Value>| {
        payload
            .and_then(|value| value.get("total_bytes").and_then(Value::as_u64))
            .unwrap_or(0)
    };
    let before = list_total(
        rpc(ctx, "models.list", json!({ "verify_hash": false }))
            .await
            .ok(),
    );
    let mut deleted: Vec<String> = Vec::new();
    let mut failures: Vec<Value> = Vec::new();
    for model_id in &request.model_ids {
        match rpc(ctx, "models.delete", json!({ "id": model_id })).await {
            Ok(response) => {
                if response
                    .get("deleted")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    deleted.push(model_id.clone());
                } else {
                    failures.push(
                        json!({ "modelId": model_id, "error": "delete non eseguito dal motore" }),
                    );
                }
            }
            Err(error) => failures.push(json!({ "modelId": model_id, "error": error })),
        }
    }
    let _ = rpc(ctx, "models.prune", json!({})).await;
    let after = list_total(
        rpc(ctx, "models.list", json!({ "verify_hash": false }))
            .await
            .ok(),
    );
    let _ = ctx
        .app
        .emit("models://changed", json!({ "reason": "delete" }));
    Ok(json!({
        "deleted": deleted,
        "failures": failures,
        "freedBytes": before.saturating_sub(after),
    }))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ClearCacheRequest {
    #[serde(default)]
    pub scope: Option<String>,
}

#[tauri::command]
pub async fn clear_cache(
    state: State<'_, AppCtx>,
    request: ClearCacheRequest,
) -> NlResult<Value> {
    let ctx = state.inner();
    let _ = request.scope;
    let response = rpc(ctx, "cache.clear", json!({})).await?;
    let freed = response
        .get("removed_bytes")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let _ = rpc(ctx, "models.prune", json!({})).await;
    let _ = ctx
        .app
        .emit("models://changed", json!({ "reason": "cache" }));
    Ok(json!({ "freedBytes": freed, "cleared": response }))
}

async fn model_settings_value(ctx: &AppCtx) -> Value {
    let config = ctx.config_snapshot().await;
    let host = paths::resolve_paths(&ctx.app, &config);
    let mut models_dir = host.models_dir.clone();
    let mut cache_dir = host.cache_dir.clone();
    let mut downloaded_bytes = 0u64;
    let mut cache_bytes = 0u64;
    let mut engine_error: Option<NlError> = None;

    if ctx.engine.is_running().await {
        match ctx
            .engine
            .request("settings.get", json!({}), timeout_for("settings.get"))
            .await
        {
            Ok(payload) => {
                if let Some(settings) = payload.get("settings") {
                    if let Some(value) = settings.get("models_dir_resolved").and_then(Value::as_str) {
                        if !value.is_empty() {
                            models_dir = value.to_string();
                        }
                    }
                    if let Some(value) = settings.get("cache_dir_resolved").and_then(Value::as_str) {
                        if !value.is_empty() {
                            cache_dir = value.to_string();
                        }
                    }
                }
            }
            Err(error) => engine_error = Some(error),
        }
        if engine_error.is_none() {
            if let Ok(cache) = ctx
                .engine
                .request("cache.size", json!({}), timeout_for("cache.size"))
                .await
            {
                cache_bytes = cache.get("size_bytes").and_then(Value::as_u64).unwrap_or(0);
            }
            if let Ok(list) = ctx
                .engine
                .request(
                    "models.list",
                    json!({ "verify_hash": false }),
                    timeout_for("models.list"),
                )
                .await
            {
                downloaded_bytes = list.get("total_bytes").and_then(Value::as_u64).unwrap_or(0);
            }
        }
    } else {
        engine_error = Some(NlError::new(
            "ENGINE_EXITED",
            "Motore non collegato: percorsi e dimensioni provengono dalla configurazione host.",
        ));
    }

    json!({
        "dataDir": host.data_dir,
        "modelsDir": models_dir,
        "cacheDir": cache_dir,
        "portable": host.portable,
        "portableAvailable": host.portable_available,
        "portableReason": host.portable_reason,
        "downloadedBytes": downloaded_bytes,
        "cacheBytes": cache_bytes,
        "writable": paths::is_dir_writable(Path::new(&models_dir)),
        "engineError": engine_error,
    })
}

#[tauri::command]
pub async fn get_model_settings(state: State<'_, AppCtx>) -> NlResult<Value> {
    Ok(model_settings_value(state.inner()).await)
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SetModelSettingsRequest {
    #[serde(default)]
    pub models_dir: Option<String>,
    #[serde(default)]
    pub cache_dir: Option<String>,
    #[serde(default)]
    pub portable: Option<bool>,
}

#[tauri::command]
pub async fn set_model_settings(
    state: State<'_, AppCtx>,
    request: SetModelSettingsRequest,
) -> NlResult<Value> {
    let ctx = state.inner();

    if let Some(value) = request.models_dir.as_deref() {
        let trimmed = value.trim();
        if !trimmed.is_empty() && !paths::is_dir_writable(Path::new(trimmed)) {
            return Err(NlError::new(
                "MODEL_PATH_NOT_WRITABLE",
                format!("Cartella modelli non scrivibile: {trimmed}"),
            ));
        }
    }
    if let Some(value) = request.cache_dir.as_deref() {
        let trimmed = value.trim();
        if !trimmed.is_empty() && !paths::is_dir_writable(Path::new(trimmed)) {
            return Err(NlError::new(
                "CACHE_PATH_NOT_WRITABLE",
                format!("Cartella cache non scrivibile: {trimmed}"),
            ));
        }
    }
    if request.portable == Some(true) {
        let available = paths::exe_dir()
            .map(|dir| paths::is_dir_writable(&dir))
            .unwrap_or(false);
        if !available {
            return Err(NlError::new(
                "PORTABLE_UNAVAILABLE",
                "La cartella dell'eseguibile non è scrivibile: modalità portable non disponibile.",
            ));
        }
    }

    let mut engine_patch = serde_json::Map::new();
    {
        let mut config = ctx.config.lock().await;
        if let Some(value) = request.models_dir.as_deref() {
            let trimmed = value.trim();
            config.models_dir_override = if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            };
            engine_patch.insert(
                "models_dir".to_string(),
                if trimmed.is_empty() {
                    Value::Null
                } else {
                    json!(trimmed)
                },
            );
        }
        if let Some(value) = request.cache_dir.as_deref() {
            let trimmed = value.trim();
            config.cache_dir_override = if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            };
            engine_patch.insert(
                "cache_dir".to_string(),
                if trimmed.is_empty() {
                    Value::Null
                } else {
                    json!(trimmed)
                },
            );
        }
        if let Some(portable) = request.portable {
            config.portable = Some(portable);
        }
        config.save(&ctx.config_path)?;
    }

    if !engine_patch.is_empty() {
        rpc(
            ctx,
            "settings.set",
            json!({ "settings": Value::Object(engine_patch) }),
        )
        .await?;
    }
    let _ = ctx
        .app
        .emit("models://changed", json!({ "reason": "settings" }));
    Ok(model_settings_value(ctx).await)
}
/* -------------------------------- updates -------------------------------- */

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CheckUpdatesRequest {
    #[serde(default)]
    pub force: bool,
}

fn cache_is_fresh(last_check: Option<&str>) -> bool {
    let Some(last_check) = last_check else {
        return false;
    };
    let Ok(parsed) = chrono::DateTime::parse_from_rfc3339(last_check) else {
        return false;
    };
    let age = chrono::Utc::now().signed_duration_since(parsed.with_timezone(&chrono::Utc));
    age.num_hours() < 24
}

#[derive(Debug, Deserialize)]
pub struct AiInstallRequest {
    pub variant: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AiUninstallRequest {
    #[serde(default)]
    pub remove_models: bool,
}

#[tauri::command]
pub async fn ai_status(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "ai.status", json!({})).await
}

#[tauri::command]
pub async fn ai_install(
    state: State<'_, AppCtx>,
    request: AiInstallRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "ai.install",
        json!({ "variant": request.variant, "confirmed": true }),
    )
    .await
}

#[tauri::command]
pub async fn ai_uninstall(
    state: State<'_, AppCtx>,
    request: AiUninstallRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "ai.uninstall",
        json!({ "remove_models": request.remove_models, "confirmed": true }),
    )
    .await
}

#[tauri::command]
pub async fn ai_verify(state: State<'_, AppCtx>) -> NlResult<Value> {
    rpc(state.inner(), "ai.verify", json!({})).await
}

#[tauri::command]
pub async fn check_updates(
    state: State<'_, AppCtx>,
    request: CheckUpdatesRequest,
) -> NlResult<UpdateInfo> {
    let ctx = state.inner();
    let config = ctx.config_snapshot().await;

    if !request.force {
        if let Some(cached) = config.cached_update.as_ref() {
            if cache_is_fresh(config.last_update_check.as_deref()) {
                if let Ok(info) = serde_json::from_value::<UpdateInfo>(cached.clone()) {
                    return Ok(info);
                }
            }
        }
    }

    let current = ctx.app.package_info().version.to_string();
    let info = updates::check(&ctx.http, &current).await?;
    {
        let mut config = ctx.config.lock().await;
        config.cached_update = serde_json::to_value(&info).ok();
        config.last_update_check = info.checked_at.clone();
        config.save(&ctx.config_path)?;
    }
    Ok(info)
}

/* ------------------------------- laboratorio ------------------------------ */
/* Bridge verso il contratto engine-core: lab.* (Qwen3-TTS + Whisper).
   Il backend può non essere ancora presente: l'errore RPC METHOD_NOT_FOUND
   viene restituito alla UI come motivazione reale, mai come "non disponibile". */

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LabInstallModelRequest {
    pub id: String,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub local_path: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct LabModelIdRequest {
    pub id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LabInstallWhisperRequest {
    pub model: String,
    #[serde(default)]
    pub local_path: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct LabWhisperRequest {
    pub model: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LabTranscribeRequest {
    pub audio: String,
    pub model: String,
    #[serde(default)]
    pub language: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LabRunRequest {
    pub original: String,
    pub protected: String,
    pub model_id: String,
    pub text: String,
    #[serde(default)]
    pub ref_text: Option<String>,
    #[serde(default)]
    pub language: Option<String>,
    #[serde(default)]
    pub seed: Option<i64>,
    #[serde(default)]
    pub device: Option<String>,
    #[serde(default)]
    pub output_dir: Option<String>,
}

#[cfg(windows)]
fn disk_free_bytes(path: &str) -> Option<u64> {
    use std::os::windows::ffi::OsStrExt;
    #[link(name = "kernel32")]
    extern "system" {
        fn GetDiskFreeSpaceExW(
            directory: *const u16,
            free_to_caller: *mut u64,
            total: *mut u64,
            total_free: *mut u64,
        ) -> i32;
    }
    let wide: Vec<u16> = std::ffi::OsStr::new(path)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let mut free = 0u64;
    let mut total = 0u64;
    let mut total_free = 0u64;
    let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut free, &mut total, &mut total_free) };
    if ok != 0 {
        Some(free)
    } else {
        None
    }
}

#[cfg(not(windows))]
fn disk_free_bytes(_path: &str) -> Option<u64> {
    None
}

#[tauri::command]
pub async fn lab_status(state: State<'_, AppCtx>) -> NlResult<Value> {
    let mut value = rpc(state.inner(), "lab.status", json!({})).await?;
    let probe = value
        .pointer("/lab/paths/models_dir")
        .and_then(Value::as_str)
        .or_else(|| value.pointer("/lab/paths/data_dir").and_then(Value::as_str))
        .map(str::to_string);
    if let Some(free) = probe.and_then(|path| disk_free_bytes(&path)) {
        if let Some(lab) = value.get_mut("lab").and_then(Value::as_object_mut) {
            lab.insert("free_bytes".to_string(), json!(free));
        }
    }
    Ok(value)
}

#[tauri::command]
pub async fn lab_verify(state: State<'_, AppCtx>, deep: Option<bool>) -> NlResult<Value> {
    rpc(
        state.inner(),
        "lab.verify",
        json!({ "deep": deep.unwrap_or(false) }),
    )
    .await
}

#[tauri::command]
pub async fn lab_install_model(
    state: State<'_, AppCtx>,
    request: LabInstallModelRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "lab.install_model",
        json!({
            "id": request.id,
            "source": request.source.unwrap_or_else(|| "download".to_string()),
            "local_path": request.local_path,
        }),
    )
    .await
}

#[tauri::command]
pub async fn lab_uninstall_model(
    state: State<'_, AppCtx>,
    request: LabModelIdRequest,
) -> NlResult<Value> {
    rpc(state.inner(), "lab.uninstall_model", json!({ "id": request.id })).await
}

#[tauri::command]
pub async fn lab_install_whisper(
    state: State<'_, AppCtx>,
    request: LabInstallWhisperRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "lab.install_whisper",
        json!({
            "size": request.model,
            "local_path": request.local_path,
        }),
    )
    .await
}

#[tauri::command]
pub async fn lab_uninstall_whisper(
    state: State<'_, AppCtx>,
    request: LabWhisperRequest,
) -> NlResult<Value> {
    rpc(state.inner(), "lab.uninstall_whisper", json!({ "model": request.model })).await
}

#[tauri::command]
pub async fn lab_transcribe(
    state: State<'_, AppCtx>,
    request: LabTranscribeRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "lab.transcribe",
        json!({
            "audio": request.audio,
            "model": request.model,
            "language": request.language.unwrap_or_else(|| "it".to_string()),
        }),
    )
    .await
}

#[tauri::command]
pub async fn lab_run(
    state: State<'_, AppCtx>,
    request: LabRunRequest,
) -> NlResult<Value> {
    rpc(
        state.inner(),
        "lab.run",
        json!({
            "original": request.original,
            "protected": request.protected,
            "model_id": request.model_id,
            "text": request.text,
            "ref_text": request.ref_text.unwrap_or_default(),
            "language": request.language.unwrap_or_else(|| "Italian".to_string()),
            "seed": request.seed.unwrap_or(0),
            "device": request.device.unwrap_or_else(|| "cpu".to_string()),
            "output_dir": request.output_dir,
        }),
    )
    .await
}
