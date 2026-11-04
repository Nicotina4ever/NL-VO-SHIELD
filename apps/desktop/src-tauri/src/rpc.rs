//! Bridge JSON-RPC 2.0 su stdio (NDJSON) verso il sidecar Python.
//!
//! - un solo processo motore, avviato al primo uso e riavviabile;
//! - mappa `id → oneshot` per correlare le risposte;
//! - le notifiche `job.progress` / `job.finished` / `job.failed` sono inoltrate
//!   come eventi Tauri (`engine://progress`, `engine://finished`, `engine://failed`);
//! - timeout per richiesta, errori strutturati, nessun panic.

use crate::error::{NlError, NlResult};
use crate::paths::{EngineEntry, EngineLaunch};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};
use tokio::task::JoinHandle;

pub const MAX_LOG_LINES: usize = 800;
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(120);
const RESTART_BACKOFF: Duration = Duration::from_millis(1500);

/// Destinazione degli eventi (AppHandle in produzione, raccolta nei test).
pub trait EventSink: Send + Sync + 'static {
    fn emit(&self, event: &str, payload: Value);
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    pub ts: String,
    pub level: String,
    pub line: String,
}

fn now_rfc3339() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// Timeout per metodo: `job.start` risponde subito, le analisi audio possono durare.
pub fn timeout_for(method: &str) -> Duration {
    match method {
        "health" => Duration::from_secs(120),
        "verify.run" | "quality.analyze" => Duration::from_secs(240),
        "doctor.run" => Duration::from_secs(120),
        "job.start" | "batch.start" => Duration::from_secs(45),
        "job.cancel" => Duration::from_secs(20),
        "lab.status" => Duration::from_secs(90),
        "lab.install_model" | "lab.install_whisper" => Duration::from_secs(3600),
        "lab.uninstall_model" | "lab.uninstall_whisper" => Duration::from_secs(120),
        "lab.transcribe" => Duration::from_secs(1800),
        "lab.run" => Duration::from_secs(120),
        "components.action" | "micromamba.install" | "micromamba.uninstall"
        | "micromamba.create_env" | "micromamba.remove_env" => Duration::from_secs(7200),
        "components.list" | "micromamba.status" | "micromamba.verify" => Duration::from_secs(180),
        "lab.verify" => Duration::from_secs(900),
        _ => Duration::from_secs(30),
    }
}

fn is_retryable(method: &str) -> bool {
    matches!(
        method,
        "health"
            | "job.status"
            | "job.list"
            | "doctor.run"
            | "settings.get"
            | "profiles.list"
            | "presets.list"
            | "receipts.list"
    ) || method.starts_with("engines.")
}

struct Runtime {
    child: Child,
    stdin: ChildStdin,
    reader: JoinHandle<()>,
    stderr: JoinHandle<()>,
}

type PendingMap = Arc<Mutex<HashMap<String, oneshot::Sender<Result<Value, NlError>>>>>;

pub struct EngineManager {
    sink: Arc<dyn EventSink>,
    pending: PendingMap,
    runtime: Mutex<Option<Runtime>>,
    launch: Mutex<Option<EngineLaunch>>,
    spawn_lock: Mutex<()>,
    logs: Arc<Mutex<VecDeque<LogEntry>>>,
    health: Mutex<Option<Value>>,
    next_id: AtomicU64,
    last_spawn: Mutex<Option<Instant>>,
}

impl EngineManager {
    pub fn new(sink: Arc<dyn EventSink>) -> Self {
        Self {
            sink,
            pending: Arc::new(Mutex::new(HashMap::new())),
            runtime: Mutex::new(None),
            launch: Mutex::new(None),
            spawn_lock: Mutex::new(()),
            logs: Arc::new(Mutex::new(VecDeque::new())),
            health: Mutex::new(None),
            next_id: AtomicU64::new(0),
            last_spawn: Mutex::new(None),
        }
    }

    fn emit(&self, event: &str, payload: Value) {
        self.sink.emit(event, payload);
    }

    /// Imposta (o aggiorna) la descrizione del processo. Ritorna `true` se è
    /// cambiata e quindi il motore va riavviato.
    pub async fn set_launch(&self, launch: EngineLaunch) -> bool {
        let mut guard = self.launch.lock().await;
        let changed = guard
            .as_ref()
            .map(|current| current.version_token() != launch.version_token())
            .unwrap_or(true);
        if changed {
            *guard = Some(launch);
        }
        changed
    }

    pub async fn is_running(&self) -> bool {
        self.is_alive().await
    }

    async fn is_alive(&self) -> bool {
        let mut guard = self.runtime.lock().await;
        if let Some(runtime) = guard.as_mut() {
            match runtime.child.try_wait() {
                Ok(None) => return true,
                _ => {
                    guard.take();
                    return false;
                }
            }
        }
        false
    }

    async fn stderr_tail(&self, lines: usize) -> String {
        let guard = self.logs.lock().await;
        guard
            .iter()
            .filter(|entry| entry.level == "stderr")
            .rev()
            .take(lines)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .map(|entry| entry.line.clone())
            .collect::<Vec<_>>()
            .join("\n")
    }

    /// Ferma il processo (kill) e libera le richieste in attesa.
    pub async fn stop(&self) {
        let taken = {
            let mut guard = self.runtime.lock().await;
            guard.take()
        };
        if let Some(runtime) = taken {
            let Runtime {
                mut child,
                reader,
                stderr,
                stdin,
            } = runtime;
            reader.abort();
            stderr.abort();
            drop(stdin);
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        *self.health.lock().await = None;
        let mut pending = self.pending.lock().await;
        for (_, sender) in pending.drain() {
            let _ = sender.send(Err(NlError::new(
                "ENGINE_EXITED",
                "Il processo del motore è stato fermato.",
            )));
        }
        self.emit("engine://status", json!({ "state": "stopped" }));
    }

    /// Garantisce un processo vivo e un handshake `health` completato.
    pub async fn ensure_ready(&self) -> NlResult<Value> {
        let _guard = self.spawn_lock.lock().await;
        if self.is_alive().await {
            if let Some(health) = self.health.lock().await.clone() {
                return Ok(health);
            }
            let health = self
                .send_request("health", json!({}), timeout_for("health"))
                .await?;
            *self.health.lock().await = Some(health.clone());
            return Ok(health);
        }

        self.stop().await;
        let launch = self.launch.lock().await.clone().ok_or_else(|| {
            NlError::new(
                "ENGINE_NOT_CONFIGURED",
                "Nessun motore configurato: interprete Python non risolto.",
            )
        })?;

        {
            let mut last = self.last_spawn.lock().await;
            if let Some(instant) = *last {
                let elapsed = instant.elapsed();
                if elapsed < RESTART_BACKOFF {
                    tokio::time::sleep(RESTART_BACKOFF - elapsed).await;
                }
            }
            *last = Some(Instant::now());
        }

        self.emit(
            "engine://status",
            json!({ "state": "starting", "message": "Avvio del motore Python", "detail": { "entry": entry_label(&launch.entry), "source": launch.source } }),
        );
        self.spawn_process(&launch).await?;

        match self
            .send_request("health", json!({}), HANDSHAKE_TIMEOUT)
            .await
        {
            Ok(health) => {
                *self.health.lock().await = Some(health.clone());
                self.emit(
                    "engine://status",
                    json!({ "state": "ready", "message": "Motore collegato" }),
                );
                Ok(health)
            }
            Err(error) => {
                let tail = self.stderr_tail(20).await;
                self.stop().await;
                Err(NlError::new(
                    "ENGINE_START_FAILED",
                    format!(
                        "Il motore non ha completato l'handshake: {}",
                        error.message
                    ),
                )
                .with_detail(json!({
                    "entry": entry_label(&launch.entry),
                    "source": launch.source,
                    "stderrTail": tail,
                })))
            }
        }
    }

    async fn spawn_process(&self, launch: &EngineLaunch) -> NlResult<()> {
        // L'entrypoint è sempre esplicito: launcher nlvs_rpc.py, binario frozen o
        // fallback `-m` per interpreti non isolati. Col contratto attuale l'interprete
        // di progetto è isolato, quindi `-m` e PYTHONPATH non sono la via principale.
        let mut command = match &launch.entry {
            EngineEntry::Script { python, script } => {
                let mut command = Command::new(python);
                command.arg(script);
                command
            }
            EngineEntry::Module { python, module } => {
                let mut command = Command::new(python);
                command.arg("-m").arg(module);
                command
            }
            EngineEntry::Frozen { exe } => {
                // The frozen sidecar entrypoint (tooling/sidecar_entry.py) starts
                // the JSON-RPC loop only with `--rpc`; without it the CLI parser
                // exits with "the following arguments are required: command".
                let mut command = Command::new(exe);
                command.arg("--rpc");
                command
            }
        };
        command.arg("--data-dir").arg(&launch.data_dir);
        command.current_dir(&launch.cwd);
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        command
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUTF8", "1")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("NLVS_DATA_DIR", &launch.data_dir)
            .env("NLVS_MODELS_DIR", &launch.models_dir)
            .env("NLVS_CACHE_DIR", &launch.cache_dir);
        if let Some(root) = launch.project_root.as_ref() {
            command.env("NLVS_PROJECT_ROOT", root);
        }
        if let Some(c2patool) = launch.c2patool.as_ref() {
            // Binario incluso nel pacchetto: percorso assoluto passato al motore.
            command.env("NLVS_C2PATOOL", c2patool);
        }
        // Best effort: innocuo con interpreti isolati, utile con runtime standard.
        if !launch.module_roots.is_empty() {
            let mut parts: Vec<String> = launch
                .module_roots
                .iter()
                .map(|path| path.display().to_string())
                .collect();
            if let Some(existing) = std::env::var_os("PYTHONPATH") {
                let existing = existing.to_string_lossy().to_string();
                if !existing.is_empty() {
                    parts.push(existing);
                }
            }
            command.env("PYTHONPATH", parts.join(PATH_SEPARATOR));
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.as_std_mut().creation_flags(CREATE_NO_WINDOW);
        }

        let entry_text = entry_label(&launch.entry);

        let mut child = command.spawn().map_err(|error| {
            NlError::new(
                "ENGINE_SPAWN_FAILED",
                format!("Impossibile avviare il motore Python: {error}"),
            )
            .with_detail(json!({
                "entry": entry_text,
                "cwd": launch.cwd.display().to_string(),
            }))
        })?;

        let stdin = child.stdin.take().ok_or_else(|| {
            NlError::new("ENGINE_SPAWN_FAILED", "stdin del motore non disponibile.")
        })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            NlError::new("ENGINE_SPAWN_FAILED", "stdout del motore non disponibile.")
        })?;
        let stderr = child.stderr.take().ok_or_else(|| {
            NlError::new("ENGINE_SPAWN_FAILED", "stderr del motore non disponibile.")
        })?;

        let pending = self.pending.clone();
        let sink = self.sink.clone();
        let reader = tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            loop {
                match lines.next_line().await {
                    Ok(Some(line)) => handle_line(&line, &pending, &sink).await,
                    Ok(None) => break,
                    Err(_) => break,
                }
            }
            let mut guard = pending.lock().await;
            let keys: Vec<String> = guard.keys().cloned().collect();
            for key in keys {
                if let Some(sender) = guard.remove(&key) {
                    let _ = sender.send(Err(NlError::new(
                        "ENGINE_EXITED",
                        "Il processo del motore è terminato prima della risposta.",
                    )));
                }
            }
            drop(guard);
            sink.emit(
                "engine://status",
                json!({ "state": "crashed", "message": "Il processo del motore è terminato." }),
            );
        });

        let logs = self.logs.clone();
        let stderr_sink = self.sink.clone();
        let stderr_task = tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let entry = LogEntry {
                    ts: now_rfc3339(),
                    level: "stderr".to_string(),
                    line: line.clone(),
                };
                {
                    let mut guard = logs.lock().await;
                    guard.push_back(entry.clone());
                    while guard.len() > MAX_LOG_LINES {
                        guard.pop_front();
                    }
                }
                stderr_sink.emit(
                    "engine://log",
                    json!({ "ts": entry.ts, "level": entry.level, "line": entry.line }),
                );
            }
        });

        {
            let mut guard = self.runtime.lock().await;
            *guard = Some(Runtime {
                child,
                stdin,
                reader,
                stderr: stderr_task,
            });
        }
        self.emit(
            "engine://status",
            json!({ "state": "starting", "message": "Processo motore avviato, attesa handshake" }),
        );
        Ok(())
    }

    /// Invio di una richiesta senza garantire lo stato del processo.
    async fn send_request(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> NlResult<Value> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst) + 1;
        let key = id.to_string();
        let (sender, receiver) = oneshot::channel();
        self.pending.lock().await.insert(key.clone(), sender);

        let payload = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });
        let mut line = serde_json::to_string(&payload)?;
        line.push('\n');

        let write_result: Result<(), NlError> = {
            let mut guard = self.runtime.lock().await;
            match guard.as_mut() {
                None => Err(NlError::new(
                    "ENGINE_EXITED",
                    "Il motore non è in esecuzione.",
                )),
                Some(runtime) => match runtime.stdin.write_all(line.as_bytes()).await {
                    Ok(_) => match runtime.stdin.flush().await {
                        Ok(_) => Ok(()),
                        Err(error) => Err(NlError::new(
                            "ENGINE_WRITE_FAILED",
                            format!("Invio al motore fallito: {error}"),
                        )),
                    },
                    Err(error) => Err(NlError::new(
                        "ENGINE_WRITE_FAILED",
                        format!("Invio al motore fallito: {error}"),
                    )),
                },
            }
        };

        if let Err(error) = write_result {
            self.pending.lock().await.remove(&key);
            return Err(error);
        }

        match tokio::time::timeout(timeout, receiver).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(NlError::new(
                "ENGINE_EXITED",
                "Il processo del motore è terminato prima della risposta.",
            )),
            Err(_) => {
                self.pending.lock().await.remove(&key);
                Err(NlError::timeout(method, timeout.as_secs()))
            }
        }
    }

    /// Richiesta pubblica: garantisce il processo, con un retry per i metodi
    /// idempotenti dopo un crash del sidecar.
    pub async fn request(&self, method: &str, params: Value, timeout: Duration) -> NlResult<Value> {
        self.ensure_ready().await?;
        let attempt = self.send_request(method, params.clone(), timeout).await;
        if let Err(error) = &attempt {
            let transient = matches!(error.code.as_str(), "ENGINE_EXITED" | "ENGINE_WRITE_FAILED");
            if transient && is_retryable(method) {
                self.stop().await;
                self.ensure_ready().await?;
                return self.send_request(method, params, timeout).await;
            }
        }
        attempt
    }

    pub async fn health(&self) -> NlResult<Value> {
        self.ensure_ready().await
    }

    pub async fn restart(&self) -> NlResult<Value> {
        self.stop().await;
        self.ensure_ready().await
    }

    pub async fn logs(&self, limit: usize) -> Vec<LogEntry> {
        let guard = self.logs.lock().await;
        let skip = guard.len().saturating_sub(limit);
        guard.iter().skip(skip).cloned().collect()
    }

    pub async fn clear_logs(&self) {
        self.logs.lock().await.clear();
    }

    /// Arresto controllato del sidecar (best effort, usato alla chiusura dell'app).
    pub async fn shutdown(&self) {
        let _ = tokio::time::timeout(
            Duration::from_secs(3),
            self.send_request("shutdown", json!({}), Duration::from_secs(2)),
        )
        .await;
        self.stop().await;
    }
}

fn entry_label(entry: &EngineEntry) -> String {
    match entry {
        EngineEntry::Script { python, script } => {
            format!("{} {}", python.display(), script.display())
        }
        EngineEntry::Module { python, module } => format!("{} -m {}", python.display(), module),
        EngineEntry::Frozen { exe } => exe.display().to_string(),
    }
}

#[cfg(windows)]
const PATH_SEPARATOR: &str = ";";
#[cfg(not(windows))]
const PATH_SEPARATOR: &str = ":";

fn id_key(value: &Value) -> Option<String> {
    match value {
        Value::Number(number) => Some(number.to_string()),
        Value::String(text) => Some(text.clone()),
        _ => None,
    }
}

async fn handle_line(line: &str, pending: &PendingMap, sink: &Arc<dyn EventSink>) {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return;
    }
    let parsed: Value = match serde_json::from_str(trimmed) {
        Ok(value) => value,
        Err(_) => {
            sink.emit(
                "engine://log",
                json!({ "ts": now_rfc3339(), "level": "stdout", "line": trimmed }),
            );
            return;
        }
    };

    if let Some(key) = parsed.get("id").and_then(id_key) {
        let sender = pending.lock().await.remove(&key);
        if let Some(sender) = sender {
            if let Some(error) = parsed.get("error") {
                let code = error.get("code").and_then(Value::as_i64).unwrap_or(-32603);
                let message = error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("Errore del motore");
                let detail = {
                    let mut detail = error.get("data").cloned().unwrap_or(Value::Null);
                    if detail.is_null() {
                        detail = json!({ "rpcCode": code });
                    } else if let Value::Object(ref mut map) = detail {
                        map.insert("rpcCode".to_string(), json!(code));
                    }
                    detail
                };
                let _ = sender.send(Err(
                    NlError::new("ENGINE_RPC_ERROR", message.to_string()).with_detail(detail)
                ));
            } else {
                let result = parsed.get("result").cloned().unwrap_or(Value::Null);
                let _ = sender.send(Ok(result));
            }
        }
        return;
    }

    if let Some(method) = parsed.get("method").and_then(Value::as_str) {
        let params = parsed.get("params").cloned().unwrap_or_else(|| json!({}));
        let event = match method {
            "job.progress" => Some("engine://progress"),
            "job.finished" => Some("engine://finished"),
            "job.failed" => Some("engine://failed"),
            "job.log" => Some("engine://log"),
            "models.progress" => Some("models://progress"),
            "models.finished" => Some("models://finished"),
            "models.failed" => Some("models://failed"),
            "lab.progress" => Some("lab://progress"),
            "lab.finished" => Some("lab://finished"),
            "lab.failed" => Some("lab://failed"),
            _ => None,
        };
        if let Some(event) = event {
            sink.emit(event, params);
        } else {
            sink.emit(
                "engine://log",
                json!({
                    "ts": now_rfc3339(),
                    "level": "event",
                    "line": format!("{method}: {}", params),
                }),
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paths::PythonSource;
    use std::path::PathBuf;
    use std::sync::Mutex as StdMutex;

    #[derive(Default)]
    struct CollectSink {
        events: StdMutex<Vec<(String, Value)>>,
    }

    impl CollectSink {
        fn events(&self) -> Vec<(String, Value)> {
            self.events.lock().unwrap().clone()
        }
    }

    impl EventSink for CollectSink {
        fn emit(&self, event: &str, payload: Value) {
            self.events
                .lock()
                .unwrap()
                .push((event.to_string(), payload));
        }
    }

    const STUB: &str = r#"
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "health":
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"version": "stub-1.0", "python": sys.version.split()[0]}}), flush=True)
    elif method == "job.start":
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"job_id": "job-1"}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "job.progress", "params": {"job_id": "job-1", "step": "watermark", "step_index": 2, "pct": 42}}), flush=True)
    elif method == "die":
        sys.exit(0)
    elif method == "slow":
        pass
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found", "data": {"method": method}}}), flush=True)
"#;

    fn stub_launch(python: PathBuf, dir: &std::path::Path) -> EngineLaunch {
        let script = dir.join("nlvs_stub_engine.py");
        EngineLaunch {
            entry: EngineEntry::Script {
                python,
                script: script.clone(),
            },
            source: PythonSource::Env,
            module_roots: vec![dir.to_path_buf()],
            cwd: dir.to_path_buf(),
            data_dir: dir.join("data"),
            models_dir: dir.join("models"),
            cache_dir: dir.join("cache"),
            project_root: None,
            c2patool: None,
        }
    }

    fn find_python() -> Option<PathBuf> {
        if let Ok(value) = std::env::var("NLVS_ENGINE_PYTHON") {
            let path = PathBuf::from(value);
            if path.is_file() {
                return Some(path);
            }
        }
        let repo = crate::paths::find_repo_root()?;
        crate::paths::find_dev_python(&repo)
    }

    #[tokio::test]
    async fn transport_roundtrip_events_timeout_and_restart() {
        let Some(python) = find_python() else {
            eprintln!("SKIP: nessun interprete Python di progetto trovato per il test di trasporto");
            return;
        };
        let dir = std::env::temp_dir().join(format!("nlvs-rpc-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("nlvs_stub_engine.py"), STUB).unwrap();

        let sink = Arc::new(CollectSink::default());
        let manager = EngineManager::new(sink.clone());
        manager.set_launch(stub_launch(python, &dir)).await;

        let health = manager.request("health", json!({}), timeout_for("health")).await.unwrap();
        assert_eq!(health["version"], "stub-1.0");

        let started = manager
            .request("job.start", json!({ "input_path": "a.wav" }), timeout_for("job.start"))
            .await
            .unwrap();
        assert_eq!(started["job_id"], "job-1");

        // la notifica deve essere inoltrata all'EventSink
        let mut forwarded = false;
        for _ in 0..50 {
            if sink
                .events()
                .iter()
                .any(|(event, payload)| event == "engine://progress" && payload["pct"] == 42)
            {
                forwarded = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(forwarded, "notifica job.progress non inoltrata");

        // errore RPC strutturato
        let error = manager
            .request("missing.method", json!({}), Duration::from_secs(10))
            .await
            .unwrap_err();
        assert_eq!(error.code, "ENGINE_RPC_ERROR");
        assert!(error.message.contains("method not found"));

        // timeout applicato
        let error = manager
            .request("slow", json!({}), Duration::from_millis(300))
            .await
            .unwrap_err();
        assert_eq!(error.code, "ENGINE_TIMEOUT");

        // crash del sidecar e riavvio automatico
        let _ = manager.request("die", json!({}), Duration::from_secs(5)).await;
        let restarted = manager.request("health", json!({}), Duration::from_secs(60)).await;
        assert!(restarted.is_ok(), "il motore non è stato riavviato dopo il crash");
        assert_eq!(restarted.unwrap()["version"], "stub-1.0");

        manager.shutdown().await;
        let _ = std::fs::remove_dir_all(&dir);
    }
}

