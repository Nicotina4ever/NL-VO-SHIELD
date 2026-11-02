//! Configurazione host persistita (preferenze UI, percorsi, cache aggiornamenti).
//!
//! Separata dalla cartella dati: vive in `app_config_dir`, così la modalità
//! portable e la scelta dei percorsi restano valide prima che la cartella dati
//! sia stata risolta.

use crate::error::NlResult;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default, rename_all = "camelCase")]
pub struct HostConfig {
    pub language: Option<String>,
    pub theme: Option<String>,
    pub output_dir: Option<String>,
    pub open_after_job: bool,
    pub auto_update_check: bool,
    /// `None` = non deciso dall'utente (usa app_data).
    pub portable: Option<bool>,
    pub data_dir_override: Option<String>,
    pub models_dir_override: Option<String>,
    pub cache_dir_override: Option<String>,
    pub last_update_check: Option<String>,
    pub cached_update: Option<Value>,
}

impl HostConfig {
    pub fn load(path: &Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        serde_json::from_str::<Self>(&text).unwrap_or_default()
    }

    /// Scrittura atomica: file temporaneo nella stessa directory + rename.
    pub fn save(&self, path: &Path) -> NlResult<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let tmp = path.with_extension("json.tmp");
        let text = serde_json::to_string_pretty(self)?;
        std::fs::write(&tmp, text)?;
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    pub fn language_or_default(&self) -> String {
        self.language.clone().unwrap_or_else(|| "it".to_string())
    }

    pub fn theme_or_default(&self) -> String {
        self.theme.clone().unwrap_or_else(|| "dark".to_string())
    }
}
