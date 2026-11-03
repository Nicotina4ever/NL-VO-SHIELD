//! NL VO Shield — host desktop Tauri 2.
//!
//! Responsabilità dell'host:
//! - finestra e asset UI (Svelte 5);
//! - risoluzione e avvio del sidecar Python (entrypoint esplicito, niente PATH);
//! - bridge JSON-RPC NDJSON → comandi tipizzati ed eventi UI;
//! - percorsi dati dentro la cartella dell'installazione, modelli, aggiornamenti.

mod commands;
mod error;
mod hardware;
mod hostconfig;
mod paths;
mod rpc;
mod updates;
mod waveform;

use commands::{AppCtx, AppSink};
use hostconfig::HostConfig;
use rpc::EngineManager;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tauri::Manager;

pub fn run() {
    // Regola di prodotto: dati dentro la cartella dell'installazione.
    // WebView2 conserva solo la sua cache: la spostiamo in <install>/data quando scrivibile.
    if let Some(install_dir) = paths::exe_dir() {
        let data_dir = install_dir.join("data");
        if paths::is_dir_writable(&data_dir) {
            let webview_dir = data_dir.join("webview2");
            if std::fs::create_dir_all(&webview_dir).is_ok() {
                std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &webview_dir);
            }
        }
    }

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let install_dir = paths::exe_dir().unwrap_or_else(|| PathBuf::from("."));
            let config_path = paths::resolve_config_path(&install_dir);
            if let Some(parent) = config_path.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|error| format!("impossibile creare {}: {error}", parent.display()))?;
            }
            let config = HostConfig::load(&config_path);

            let sink = Arc::new(AppSink {
                app: handle.clone(),
            });
            let engine = EngineManager::new(sink);
            let http = reqwest::Client::builder()
                .user_agent("NL-VO-Shield/1.0 (+https://github.com/Nicotina4ever/NL-VO-SHIELD)")
                .timeout(Duration::from_secs(60))
                .connect_timeout(Duration::from_secs(20))
                .build()
                .map_err(|error| format!("client HTTP non disponibile: {error}"))?;
            let ctx = AppCtx::new(handle.clone(), engine, config, config_path, http);
            app.manage(ctx);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::engine_health,
            commands::engine_restart,
            commands::engine_logs,
            commands::open_engine_log,
            commands::host_info,
            commands::list_engines,
            commands::engine_install_hint,
            commands::start_job,
            commands::cancel_job,
            commands::job_status,
            commands::job_list,
            commands::verify_file,
            commands::analyze_quality,
            commands::start_batch,
            commands::run_doctor,
            commands::list_profiles,
            commands::save_profile,
            commands::delete_profile,
            commands::list_presets,
            commands::save_preset,
            commands::list_receipts,
            commands::verify_receipt,
            commands::get_settings,
            commands::set_settings,
            commands::open_path,
            commands::export_report,
            commands::read_waveform,
            commands::read_file_base64,
            commands::list_models,
            commands::ai_status,
            commands::ai_install,
            commands::ai_uninstall,
            commands::ai_verify,
            commands::download_model,
            commands::cancel_model_download,
            commands::delete_model,
            commands::clear_cache,
            commands::get_model_settings,
            commands::set_model_settings,
            commands::check_updates,
            commands::lab_status,
            commands::components_list,
            commands::components_action,
            commands::micromamba_status,
            commands::micromamba_install,
            commands::micromamba_verify,
            commands::micromamba_uninstall,
            commands::micromamba_create_env,
            commands::micromamba_remove_env,
            commands::lab_install_model,
            commands::lab_uninstall_model,
            commands::lab_install_whisper,
            commands::lab_uninstall_whisper,
            commands::lab_transcribe,
            commands::lab_run,
            commands::lab_verify,
        ])
        .build(tauri::generate_context!())
        .expect("errore durante l'avvio di NL VO Shield");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(ctx) = app_handle.try_state::<AppCtx>() {
                let engine = &ctx.inner().engine;
                tauri::async_runtime::block_on(async {
                    engine.shutdown().await;
                });
            }
        }
    });
}
