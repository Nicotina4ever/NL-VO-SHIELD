//! Errori strutturati host ↔ UI: `{ code, message, detail }`, mai panic.

use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct NlError {
    pub code: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<Value>,
}

pub type NlResult<T> = Result<T, NlError>;

impl NlError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            detail: None,
        }
    }

    pub fn with_detail(mut self, detail: impl Into<Value>) -> Self {
        self.detail = Some(detail.into());
        self
    }

    pub fn timeout(operation: &str, seconds: u64) -> Self {
        Self::new(
            "ENGINE_TIMEOUT",
            format!("Il motore non ha risposto a «{operation}» entro {seconds}s."),
        )
        .with_detail(serde_json::json!({ "operation": operation, "timeoutSeconds": seconds }))
    }
}

impl std::fmt::Display for NlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for NlError {}

impl From<std::io::Error> for NlError {
    fn from(error: std::io::Error) -> Self {
        Self::new("HOST_IO_ERROR", error.to_string())
    }
}

impl From<serde_json::Error> for NlError {
    fn from(error: serde_json::Error) -> Self {
        Self::new("HOST_JSON_ERROR", error.to_string())
    }
}

impl From<tauri::Error> for NlError {
    fn from(error: tauri::Error) -> Self {
        Self::new("HOST_TAURI_ERROR", error.to_string())
    }
}
