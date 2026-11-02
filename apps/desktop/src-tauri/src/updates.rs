//! Controllo aggiornamenti: legge l'ultima GitHub Release pubblica.
//!
//! Nessun download silenzioso e nessuna auto-installazione: senza chiave di
//! firma l'updater Tauri resta disabilitato, quindi qui si espongono solo
//! versione, note e link della release. Le richieste sono a sola lettura e
//! possono essere limitate nel tempo (cache in `HostConfig`).

use crate::error::NlResult;
use serde::{Deserialize, Serialize};

pub const REPO: &str = "Nicotina4ever/NL-VO-SHIELD";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInfo {
    pub configured: bool,
    pub repo: String,
    pub current_version: String,
    pub latest_version: Option<String>,
    pub release_url: Option<String>,
    pub release_name: Option<String>,
    pub notes: Option<String>,
    pub published_at: Option<String>,
    pub up_to_date: Option<bool>,
    pub checked_at: Option<String>,
    pub reason: Option<String>,
    pub error: Option<String>,
    pub auto_update_available: bool,
}

fn base(current_version: &str) -> UpdateInfo {
    UpdateInfo {
        configured: false,
        repo: REPO.to_string(),
        current_version: current_version.to_string(),
        latest_version: None,
        release_url: None,
        release_name: None,
        notes: None,
        published_at: None,
        up_to_date: None,
        checked_at: Some(chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)),
        reason: Some(
            "Aggiornamento automatico non configurato: manca la chiave pubblica di firma. Il download resta manuale dalla release."
                .to_string(),
        ),
        error: None,
        auto_update_available: false,
    }
}

fn numeric_parts(version: &str) -> Option<Vec<u64>> {
    let cleaned = version.trim().trim_start_matches(['v', 'V']);
    let core = cleaned.split(['-', '+']).next().unwrap_or(cleaned);
    let parts: Vec<u64> = core
        .split('.')
        .map(|part| part.trim().parse::<u64>().ok())
        .collect::<Option<Vec<u64>>>()?;
    if parts.is_empty() {
        None
    } else {
        Some(parts)
    }
}

fn compare_versions(current: &str, latest: &str) -> Option<bool> {
    let a = numeric_parts(current)?;
    let b = numeric_parts(latest)?;
    let len = a.len().max(b.len());
    for index in 0..len {
        let left = *a.get(index).unwrap_or(&0);
        let right = *b.get(index).unwrap_or(&0);
        if left < right {
            return Some(false);
        }
        if left > right {
            return Some(true);
        }
    }
    Some(true)
}

pub async fn check(client: &reqwest::Client, current_version: &str) -> NlResult<UpdateInfo> {
    let mut info = base(current_version);
    let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
    let response = client
        .get(&url)
        .header("Accept", "application/vnd.github+json")
        .header(
            "User-Agent",
            "NL-VO-Shield/0.1 (+https://github.com/Nicotina4ever/NL-VO-SHIELD)",
        )
        .send()
        .await;

    let response = match response {
        Ok(response) => response,
        Err(error) => {
            info.error = Some(format!("Richiesta GitHub non riuscita: {error}"));
            return Ok(info);
        }
    };

    let status = response.status();
    if status.as_u16() == 404 {
        info.reason = Some(
            "Nessuna release pubblicata nel repository: non ci sono aggiornamenti da confrontare."
                .to_string(),
        );
        return Ok(info);
    }
    if !status.is_success() {
        info.error = Some(format!("GitHub API ha risposto HTTP {status}"));
        return Ok(info);
    }

    let payload: serde_json::Value = response
        .json()
        .await
        .map_err(|error| crate::error::NlError::new("UPDATE_PARSE_FAILED", error.to_string()))?;

    let tag = payload
        .get("tag_name")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    let Some(tag) = tag else {
        info.error = Some("La release non contiene un tag_name.".to_string());
        return Ok(info);
    };

    let latest = tag.trim().trim_start_matches(['v', 'V']).to_string();
    info.latest_version = Some(latest.clone());
    info.release_name = payload
        .get("name")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    info.release_url = payload
        .get("html_url")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    info.notes = payload
        .get("body")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    info.published_at = payload
        .get("published_at")
        .and_then(|value| value.as_str())
        .map(str::to_string);
    info.up_to_date = compare_versions(current_version, &latest);
    if info.up_to_date == Some(false) {
        info.reason = Some(format!(
            "Disponibile la versione {latest}: scaricala manualmente dalla release."
        ));
    } else if info.up_to_date == Some(true) {
        info.reason = Some("La versione installata è aggiornata.".to_string());
    }
    Ok(info)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compares_versions() {
        assert_eq!(compare_versions("0.1.0", "0.2.0"), Some(false));
        assert_eq!(compare_versions("0.2.0", "0.2.0"), Some(true));
        assert_eq!(compare_versions("1.0.0", "0.9.9"), Some(true));
        assert_eq!(compare_versions("0.1", "0.1.0"), Some(true));
        assert_eq!(compare_versions("v0.1.0", "0.1.1"), Some(false));
        assert_eq!(compare_versions("nonsense", "1.0.0"), None);
    }
}
