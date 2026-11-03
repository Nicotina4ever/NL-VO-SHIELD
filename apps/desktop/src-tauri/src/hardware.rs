//! Rilevamento hardware locale per la sezione "Protezioni consigliate" della UI.
//!
//! Regole:
//! - sola lettura, nessuna rete, nessun download, nessun percorso assoluto;
//! - nessuna dipendenza esterna: API di sistema su Windows, `/proc`/`sysctl` altrove;
//! - GPU/VRAM NVIDIA tramite `nvidia-smi` (stesso strumento usato dal runtime AI del
//!   motore Python), fallback al registro dei driver video su Windows;
//! - il risultato è rilevato una volta per processo e riusato (`OnceLock`);
//! - i campi non rilevabili restano `None` con una nota esplicita: mai valori inventati.

use serde::Serialize;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

/// Limite prudente per i processi di probe (`nvidia-smi`): un host senza driver non deve
/// mai bloccare l'apertura della pagina Motori.
const PROBE_TIMEOUT: Duration = Duration::from_millis(1500);

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HardwareInfo {
    /// `true` quando almeno CPU e RAM sono stati letti davvero.
    pub detected: bool,
    pub cpu_model: Option<String>,
    pub cpu_logical_cores: Option<u32>,
    pub ram_total_bytes: Option<u64>,
    pub gpu_name: Option<String>,
    pub gpu_vram_bytes: Option<u64>,
    pub gpu_vendor: Option<String>,
    /// Da dove arrivano i dati GPU: `nvidia-smi`, `windows-registry` o assente.
    pub gpu_source: Option<String>,
    /// Note oneste su cosa non è stato possibile rilevare.
    pub notes: Vec<String>,
}

impl HardwareInfo {
    fn empty() -> Self {
        Self {
            detected: false,
            cpu_model: None,
            cpu_logical_cores: None,
            ram_total_bytes: None,
            gpu_name: None,
            gpu_vram_bytes: None,
            gpu_vendor: None,
            gpu_source: None,
            notes: Vec::new(),
        }
    }
}

/// Rilevamento con cache: il risultato è stabile per la vita del processo.
pub fn detect() -> &'static HardwareInfo {
    static CACHE: OnceLock<HardwareInfo> = OnceLock::new();
    CACHE.get_or_init(detect_uncached)
}

fn detect_uncached() -> HardwareInfo {
    let mut info = HardwareInfo::empty();
    info.cpu_logical_cores = std::thread::available_parallelism()
        .ok()
        .map(|value| value.get() as u32);

    info.cpu_model = cpu_model();
    if info.cpu_model.is_none() {
        info.notes
            .push("CPU model not readable on this platform".to_string());
    }

    info.ram_total_bytes = ram_total_bytes();
    if info.ram_total_bytes.is_none() {
        info.notes.push("Total RAM not readable on this platform".to_string());
    }

    if let Some(gpu) = nvidia_smi_gpu() {
        info.gpu_name = Some(gpu.0);
        info.gpu_vram_bytes = gpu.1;
        info.gpu_vendor = Some("NVIDIA".to_string());
        info.gpu_source = Some("nvidia-smi".to_string());
    } else if let Some(gpu) = platform_primary_gpu() {
        info.gpu_name = Some(gpu.0);
        info.gpu_vram_bytes = gpu.1;
        info.gpu_vendor = gpu.2;
        info.gpu_source = Some("system".to_string());
    }

    if info.gpu_name.is_none() {
        info.notes.push("No GPU adapter detected (CPU-only)".to_string());
    } else if info.gpu_vram_bytes.is_none() {
        info.notes
            .push("GPU detected but dedicated VRAM not reported by the driver".to_string());
    }

    info.detected = info.cpu_model.is_some() || info.cpu_logical_cores.is_some();
    info
}

/* --------------------------- process helpers ---------------------------- */

/// Esegue un probe breve e restituisce stdout: nessun timeout bloccante oltre il limite.
fn probe_stdout(command: &str, args: &[&str], timeout: Duration) -> Option<String> {
    use std::io::Read;
    use std::process::{Command, Stdio};

    let mut command_builder = Command::new(command);
    command_builder
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: nessuna finestra console quando l'app è in esecuzione.
        command_builder.creation_flags(0x0800_0000);
    }

    let mut child = command_builder.spawn().ok()?;
    let mut stdout = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stdout.read_to_end(&mut buffer);
        buffer
    });

    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = reader.join();
                return None;
            }
            Err(_) => {
                let _ = reader.join();
                return None;
            }
        }
    }

    let bytes = reader.join().ok()?;
    String::from_utf8(bytes).ok()
}

/* ------------------------------- NVIDIA --------------------------------- */

/// `nvidia-smi --query-gpu=name,memory.total` → (nome, VRAM in byte).
fn nvidia_smi_gpu() -> Option<(String, Option<u64>)> {
    let output = probe_stdout(
        "nvidia-smi",
        &["--query-gpu=name,memory.total", "--format=csv,noheader"],
        PROBE_TIMEOUT,
    )?;
    let line = output.lines().find(|line| !line.trim().is_empty())?;
    let (name, memory) = line.rsplit_once(',')?;
    let name = name.trim().to_string();
    if name.is_empty() {
        return None;
    }
    Some((name, parse_memory_mib(memory.trim())))
}

/// Interpreta valori tipo `8188 MiB`, `16384MiB`, `12 GB`, `24576` (MiB).
fn parse_memory_mib(raw: &str) -> Option<u64> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let digits: String = raw
        .chars()
        .take_while(|ch| ch.is_ascii_digit() || *ch == '.')
        .collect();
    let value: f64 = digits.parse().ok()?;
    if !value.is_finite() || value <= 0.0 {
        return None;
    }
    let lower = raw.to_ascii_lowercase();
    let bytes = if lower.contains("gib") || lower.contains("gb") {
        value * 1024.0 * 1024.0 * 1024.0
    } else if lower.contains("mib") || lower.contains("mb") {
        value * 1024.0 * 1024.0
    } else {
        // nvidia-smi senza unità usa i MiB.
        value * 1024.0 * 1024.0
    };
    if bytes <= 0.0 {
        return None;
    }
    Some(bytes.round() as u64)
}

/* -------------------------------- Windows ------------------------------- */

#[cfg(windows)]
mod windows_impl {
    use std::ffi::c_void;

    type Hkey = *mut c_void;

    const HKEY_LOCAL_MACHINE: usize = 0x8000_0002;
    const RRF_RT_REG_SZ: u32 = 0x0000_0002;
    const RRF_RT_ANY: u32 = 0x0000_FFFF;
    const REG_DWORD: u32 = 4;
    const REG_QWORD: u32 = 11;

    #[repr(C)]
    struct MemoryStatusEx {
        length: u32,
        memory_load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page_file: u64,
        avail_page_file: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_extended_virtual: u64,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GlobalMemoryStatusEx(buffer: *mut MemoryStatusEx) -> i32;
    }

    #[link(name = "advapi32")]
    extern "system" {
        fn RegGetValueW(
            hkey: Hkey,
            sub_key: *const u16,
            value: *const u16,
            flags: u32,
            value_type: *mut u32,
            data: *mut c_void,
            data_size: *mut u32,
        ) -> i32;
    }

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn hklm() -> Hkey {
        HKEY_LOCAL_MACHINE as Hkey
    }

    /// Legge un valore REG_SZ dal registro (64 bit).
    fn read_string(sub_key: &str, value: &str) -> Option<String> {
        let sub_key = wide(sub_key);
        let value = wide(value);
        let mut size: u32 = 0;
        let status = unsafe {
            RegGetValueW(
                hklm(),
                sub_key.as_ptr(),
                value.as_ptr(),
                RRF_RT_REG_SZ,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                &mut size,
            )
        };
        if status != 0 || size == 0 {
            return None;
        }
        let mut buffer = vec![0u16; (size as usize / 2).max(1)];
        let status = unsafe {
            RegGetValueW(
                hklm(),
                sub_key.as_ptr(),
                value.as_ptr(),
                RRF_RT_REG_SZ,
                std::ptr::null_mut(),
                buffer.as_mut_ptr() as *mut c_void,
                &mut size,
            )
        };
        if status != 0 {
            return None;
        }
        let end = buffer
            .iter()
            .position(|ch| *ch == 0)
            .unwrap_or(buffer.len());
        let text = String::from_utf16_lossy(&buffer[..end]);
        let trimmed = text.trim().to_string();
        if trimmed.is_empty() {
            None
        } else {
            Some(trimmed)
        }
    }

    /// Legge un valore numerico (QWORD o DWORD) dal registro.
    fn read_u64(sub_key: &str, value: &str) -> Option<u64> {
        let sub_key = wide(sub_key);
        let value = wide(value);
        let mut size: u32 = 0;
        let mut value_type: u32 = 0;
        let status = unsafe {
            RegGetValueW(
                hklm(),
                sub_key.as_ptr(),
                value.as_ptr(),
                RRF_RT_ANY,
                &mut value_type,
                std::ptr::null_mut(),
                &mut size,
            )
        };
        if status != 0 || size == 0 || size > 16 {
            return None;
        }
        let mut buffer = [0u8; 16];
        let status = unsafe {
            RegGetValueW(
                hklm(),
                sub_key.as_ptr(),
                value.as_ptr(),
                RRF_RT_ANY,
                &mut value_type,
                buffer.as_mut_ptr() as *mut c_void,
                &mut size,
            )
        };
        if status != 0 {
            return None;
        }
        match value_type {
            REG_QWORD => Some(u64::from_le_bytes(buffer[..8].try_into().ok()?)),
            REG_DWORD if size >= 4 => Some(u32::from_le_bytes(buffer[..4].try_into().ok()?) as u64),
            _ if size == 8 => Some(u64::from_le_bytes(buffer[..8].try_into().ok()?)),
            _ => None,
        }
    }

    pub fn cpu_model() -> Option<String> {
        read_string(
            "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",
            "ProcessorNameString",
        )
        .or_else(|| {
            std::env::var("PROCESSOR_IDENTIFIER")
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
    }

    pub fn ram_total_bytes() -> Option<u64> {
        let mut status = MemoryStatusEx {
            length: std::mem::size_of::<MemoryStatusEx>() as u32,
            memory_load: 0,
            total_phys: 0,
            avail_phys: 0,
            total_page_file: 0,
            avail_page_file: 0,
            total_virtual: 0,
            avail_virtual: 0,
            avail_extended_virtual: 0,
        };
        let ok = unsafe { GlobalMemoryStatusEx(&mut status) };
        if ok == 0 || status.total_phys == 0 {
            None
        } else {
            Some(status.total_phys)
        }
    }

    /// Fallback quando `nvidia-smi` non è disponibile: adattatori video dal registro.
    pub fn primary_gpu() -> Option<(String, Option<u64>, Option<String>)> {
        const DISPLAY_CLASS: &str =
            "SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}";
        let mut first: Option<(String, Option<u64>, Option<String>)> = None;
        let mut with_vram: Option<(String, Option<u64>, Option<String>)> = None;
        for index in 0..8 {
            let sub_key = format!("{DISPLAY_CLASS}\\{index:04}");
            let Some(name) = read_string(&sub_key, "DriverDesc") else {
                continue;
            };
            let vram = read_u64(&sub_key, "HardwareInformation.qwMemorySize")
                .filter(|bytes| *bytes >= 512 * 1024 * 1024);
            let vendor = vendor_of(&name);
            let entry = (name, vram, vendor);
            if first.is_none() {
                first = Some(entry.clone());
            }
            if vram.is_some() {
                with_vram = Some(entry);
                break;
            }
        }
        with_vram.or(first)
    }

    pub fn vendor_of(name: &str) -> Option<String> {
        let lower = name.to_ascii_lowercase();
        if lower.contains("nvidia") {
            Some("NVIDIA".to_string())
        } else if lower.contains("amd") || lower.contains("radeon") {
            Some("AMD".to_string())
        } else if lower.contains("intel") || lower.contains("arc") {
            Some("Intel".to_string())
        } else if lower.contains("microsoft") || lower.contains("basic display") {
            None
        } else {
            None
        }
    }
}

#[cfg(windows)]
fn cpu_model() -> Option<String> {
    windows_impl::cpu_model()
}

#[cfg(windows)]
fn ram_total_bytes() -> Option<u64> {
    windows_impl::ram_total_bytes()
}

#[cfg(windows)]
fn platform_primary_gpu() -> Option<(String, Option<u64>, Option<String>)> {
    windows_impl::primary_gpu()
}

/* --------------------------- Linux / macOS ------------------------------ */

#[cfg(target_os = "linux")]
fn cpu_model() -> Option<String> {
    let text = std::fs::read_to_string("/proc/cpuinfo").ok()?;
    for line in text.lines() {
        if let Some(value) = line.strip_prefix("model name") {
            let value = value.split_once(':').map(|(_, right)| right).unwrap_or(value);
            let value = value.trim();
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

#[cfg(target_os = "linux")]
fn ram_total_bytes() -> Option<u64> {
    let text = std::fs::read_to_string("/proc/meminfo").ok()?;
    for line in text.lines() {
        if let Some(value) = line.strip_prefix("MemTotal:") {
            let kib: u64 = value
                .trim()
                .split_whitespace()
                .next()
                .and_then(|raw| raw.parse().ok())?;
            return Some(kib * 1024);
        }
    }
    None
}

#[cfg(target_os = "linux")]
fn platform_primary_gpu() -> Option<(String, Option<u64>, Option<String>)> {
    // Nessun registro: nvidia-smi copre NVIDIA; per gli altri vendor il nome resta noto
    // solo se `lspci` è presente. Meglio nessun dato che un dato inventato.
    let output = probe_stdout("lspci", &["-mm"], PROBE_TIMEOUT)?;
    for line in output.lines() {
        let lower = line.to_ascii_lowercase();
        if lower.contains("vga compatible controller")
            || lower.contains("3d controller")
            || lower.contains("display controller")
        {
            // Formato -mm: slot "class" "vendor" "device" ...
            let quoted: Vec<&str> = line.split('"').collect();
            if quoted.len() >= 6 {
                let vendor = quoted[3].to_string();
                let device = quoted[5].to_string();
                let name = format!("{vendor} {device}");
                return Some((name, None, Some(vendor)));
            }
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn cpu_model() -> Option<String> {
    probe_stdout("sysctl", &["-n", "machdep.cpu.brand_string"], PROBE_TIMEOUT)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

#[cfg(target_os = "macos")]
fn ram_total_bytes() -> Option<u64> {
    probe_stdout("sysctl", &["-n", "hw.memsize"], PROBE_TIMEOUT)
        .and_then(|value| value.trim().parse().ok())
}

#[cfg(target_os = "macos")]
fn platform_primary_gpu() -> Option<(String, Option<u64>, Option<String>)> {
    // Apple Silicon: GPU integrata con memoria unificata; nessuna VRAM dedicata separata.
    probe_stdout("sysctl", &["-n", "machdep.gpu.name"], PROBE_TIMEOUT)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .map(|name| (name, None, None))
}

/// Piattaforme non supportate: nessun dato inventato.
#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn cpu_model() -> Option<String> {
    None
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn ram_total_bytes() -> Option<u64> {
    None
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn platform_primary_gpu() -> Option<(String, Option<u64>, Option<String>)> {
    None
}

/* --------------------------------- tests -------------------------------- */

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_nvidia_memory_units() {
        assert_eq!(parse_memory_mib("8188 MiB"), Some(8188 * 1024 * 1024));
        assert_eq!(parse_memory_mib("12 GB"), Some(12 * 1024 * 1024 * 1024));
        assert_eq!(parse_memory_mib("24 GiB"), Some(24 * 1024 * 1024 * 1024));
        assert_eq!(parse_memory_mib(""), None);
        assert_eq!(parse_memory_mib("N/A"), None);
    }

    #[test]
    fn detects_something_on_host() {
        let info = detect();
        assert!(info.detected);
        assert!(info.cpu_logical_cores.unwrap_or(0) >= 1);
        assert!(info.ram_total_bytes.unwrap_or(0) > 0);
    }
}
