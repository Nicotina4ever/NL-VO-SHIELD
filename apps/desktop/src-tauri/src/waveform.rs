//! Forma d'onda per l'anteprima audio.
//!
//! Il parsing WAV avviene in Rust (niente decoding nel WebView per i file PCM);
//! per gli altri formati la UI prova la decodifica Web Audio e mostra un avviso
//! se non è disponibile. Nessun file viene caricato o modificato.

use crate::error::NlResult;
use serde::Serialize;
use std::fs::File;
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;

#[derive(Debug, Clone, Serialize)]
pub struct Peaks {
    pub min: Vec<f32>,
    pub max: Vec<f32>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WaveformData {
    pub supported: bool,
    pub reason: Option<String>,
    pub sample_rate: u32,
    pub channels: u16,
    pub bits_per_sample: u16,
    pub duration_sec: f64,
    pub frames: u64,
    pub peaks: Peaks,
    /// RMS per bucket (inviluppo disegnato sopra i picchi).
    pub rms: Vec<f32>,
    pub source: String,
}

fn unsupported(reason: &str) -> WaveformData {
    WaveformData {
        supported: false,
        reason: Some(reason.to_string()),
        sample_rate: 0,
        channels: 0,
        bits_per_sample: 0,
        duration_sec: 0.0,
        frames: 0,
        peaks: Peaks {
            min: Vec::new(),
            max: Vec::new(),
        },
        rms: Vec::new(),
        source: "none".to_string(),
    }
}

#[derive(Debug, Clone, Copy)]
struct Fmt {
    format_tag: u16,
    channels: u16,
    sample_rate: u32,
    block_align: u16,
    bits_per_sample: u16,
}

fn parse_fmt(buffer: &[u8]) -> Option<Fmt> {
    if buffer.len() < 16 {
        return None;
    }
    let mut format_tag = u16::from_le_bytes([buffer[0], buffer[1]]);
    let channels = u16::from_le_bytes([buffer[2], buffer[3]]);
    let sample_rate = u32::from_le_bytes([buffer[4], buffer[5], buffer[6], buffer[7]]);
    let block_align = u16::from_le_bytes([buffer[12], buffer[13]]);
    let bits_per_sample = u16::from_le_bytes([buffer[14], buffer[15]]);
    if format_tag == 0xFFFE && buffer.len() >= 26 {
        // WAVE_FORMAT_EXTENSIBLE: il sub-format inizia al byte 24
        format_tag = u16::from_le_bytes([buffer[24], buffer[25]]);
    }
    if channels == 0 || sample_rate == 0 {
        return None;
    }
    Some(Fmt {
        format_tag,
        channels,
        sample_rate,
        block_align,
        bits_per_sample,
    })
}

fn sample_at(bytes: &[u8], fmt: Fmt) -> f32 {
    match (fmt.format_tag, fmt.bits_per_sample) {
        (1, 8) => (bytes[0] as f32 - 128.0) / 128.0,
        (1, 16) => i16::from_le_bytes([bytes[0], bytes[1]]) as f32 / 32768.0,
        (1, 24) => {
            let raw = ((bytes[2] as i32) << 16) | ((bytes[1] as i32) << 8) | (bytes[0] as i32);
            let signed = if raw & 0x0080_0000 != 0 {
                raw | !0x00FF_FFFF
            } else {
                raw
            };
            signed as f32 / 8_388_608.0
        }
        (1, 32) => {
            i32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]) as f32 / 2_147_483_648.0
        }
        (3, 32) => f32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]),
        (3, 64) => f64::from_le_bytes([
            bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        ]) as f32,
        _ => 0.0,
    }
}

/// Legge un WAV PCM/float e restituisce i picchi min/max per bucket.
pub fn read_waveform(path: &Path, buckets: usize) -> NlResult<WaveformData> {
    let metadata = std::fs::metadata(path)?;
    if !metadata.is_file() {
        return Ok(unsupported("Percorso non valido"));
    }
    if metadata.len() < 44 {
        return Ok(unsupported("File troppo piccolo per essere un WAV valido"));
    }
    let mut reader = BufReader::new(File::open(path)?);
    let mut header = [0u8; 12];
    if reader.read_exact(&mut header).is_err() {
        return Ok(unsupported("Intestazione non leggibile"));
    }
    if &header[0..4] != b"RIFF" || &header[8..12] != b"WAVE" {
        return Ok(unsupported(
            "Formato diverso da WAV: la forma d'onda viene decodificata nel WebView quando supportato.",
        ));
    }

    let mut fmt: Option<Fmt> = None;
    let mut data_offset: Option<(u64, u32)> = None;
    loop {
        let mut chunk = [0u8; 8];
        if reader.read_exact(&mut chunk).is_err() {
            break;
        }
        let id = &chunk[0..4];
        let size = u32::from_le_bytes([chunk[4], chunk[5], chunk[6], chunk[7]]);
        let offset = reader.stream_position().unwrap_or(0);
        let pad = i64::from(size % 2);
        if id == b"fmt " {
            let mut buffer = vec![0u8; size.min(1024) as usize];
            if reader.read_exact(&mut buffer).is_err() {
                break;
            }
            fmt = parse_fmt(&buffer);
            if size > 1024 {
                let _ = reader.seek(SeekFrom::Current(i64::from(size) - 1024));
            }
            if pad == 1 {
                let _ = reader.seek(SeekFrom::Current(1));
            }
        } else if id == b"data" {
            data_offset = Some((offset, size));
            let _ = reader.seek(SeekFrom::Current(into_i64(size) + pad));
        } else {
            let _ = reader.seek(SeekFrom::Current(into_i64(size) + pad));
        }
        if fmt.is_some() && data_offset.is_some() {
            break;
        }
    }

    let Some(fmt) = fmt else {
        return Ok(unsupported("Chunk 'fmt ' assente: WAV non valido"));
    };
    let Some((offset, data_size)) = data_offset else {
        return Ok(unsupported("Chunk 'data' assente: WAV senza campioni"));
    };
    if !matches!((fmt.format_tag, fmt.bits_per_sample), (1, 8) | (1, 16) | (1, 24) | (1, 32) | (3, 32) | (3, 64))
    {
        return Ok(unsupported(
            "Codifica WAV non supportata per l'anteprima (solo PCM 8/16/24/32 e float).",
        ));
    }

    let bytes_per_sample = usize::from(fmt.bits_per_sample / 8);
    let block_align = if fmt.block_align >= fmt.channels * bytes_per_sample as u16 && fmt.block_align > 0
    {
        usize::from(fmt.block_align)
    } else {
        usize::from(fmt.channels) * bytes_per_sample
    };
    if block_align == 0 {
        return Ok(unsupported("Block align non valido"));
    }
    let frames = u64::from(data_size) / block_align as u64;
    if frames == 0 {
        return Ok(unsupported("WAV senza campioni"));
    }

    let bucket_count = buckets.clamp(64, 4096).min(frames as usize);
    let bucket_count = bucket_count.max(1);
    let frames_per_bucket = frames as f64 / bucket_count as f64;
    let mut mins = vec![1.0f32; bucket_count];
    let mut maxs = vec![-1.0f32; bucket_count];
    let mut sum_squares = vec![0.0f64; bucket_count];
    let mut sums_count = vec![0u32; bucket_count];

    reader.seek(SeekFrom::Start(offset))?;
    let mut buffer = vec![0u8; 256 * 1024];
    let mut remaining = data_size as usize;
    let mut frame_index: u64 = 0;

    // Buffer con "carry": i read() possono terminare a metà frame (24-bit stereo =
    // 6 byte/frame), quindi i byte avanzati vengono riportati all'inizio del buffer.
    let capacity = buffer.len();
    let mut carry = 0usize;
    while remaining > 0 || carry >= block_align {
        let space = capacity - carry;
        let to_read = remaining.min(space);
        if to_read == 0 && carry < block_align {
            break;
        }
        let read = if to_read > 0 {
            reader.read(&mut buffer[carry..carry + to_read])?
        } else {
            0
        };
        if read == 0 && to_read > 0 {
            break;
        }
        let total = carry + read;
        remaining = remaining.saturating_sub(read);

        let mut pos = 0usize;
        while pos + block_align <= total {
            let bucket = ((frame_index as f64 / frames_per_bucket) as usize).min(bucket_count - 1);
            let mut frame_min = 1.0f32;
            let mut frame_max = -1.0f32;
            let mut frame_square_sum = 0.0f64;
            let mut frame_channels = 0u32;
            for channel in 0..usize::from(fmt.channels) {
                let base = pos + channel * bytes_per_sample;
                if base + bytes_per_sample > total {
                    break;
                }
                let value = sample_at(&buffer[base..base + bytes_per_sample], fmt);
                if value < frame_min {
                    frame_min = value;
                }
                if value > frame_max {
                    frame_max = value;
                }
                frame_square_sum += f64::from(value) * f64::from(value);
                frame_channels += 1;
            }
            if frame_channels > 0 {
                sum_squares[bucket] += frame_square_sum / f64::from(frame_channels);
                sums_count[bucket] = sums_count[bucket].saturating_add(1);
            }
            if frame_min < mins[bucket] {
                mins[bucket] = frame_min;
            }
            if frame_max > maxs[bucket] {
                maxs[bucket] = frame_max;
            }
            pos += block_align;
            frame_index += 1;
        }
        carry = total - pos;
        if carry > 0 {
            buffer.copy_within(pos..total, 0);
        }
    }

    for value in mins.iter_mut() {
        *value = value.clamp(-1.0, 1.0);
        if !value.is_finite() {
            *value = 0.0;
        }
    }
    for value in maxs.iter_mut() {
        *value = value.clamp(-1.0, 1.0);
        if !value.is_finite() {
            *value = 0.0;
        }
    }

    let rms: Vec<f32> = (0..bucket_count)
        .map(|index| {
            let count = f64::from(sums_count[index]);
            if count <= 0.0 {
                0.0
            } else {
                (sum_squares[index] / count).sqrt().min(1.0) as f32
            }
        })
        .collect();

    Ok(WaveformData {
        supported: true,
        reason: None,
        sample_rate: fmt.sample_rate,
        channels: fmt.channels,
        bits_per_sample: fmt.bits_per_sample,
        duration_sec: frames as f64 / f64::from(fmt.sample_rate),
        frames,
        peaks: Peaks { min: mins, max: maxs },
        rms,
        source: "rust-wav".to_string(),
    })
}

fn into_i64(value: u32) -> i64 {
    i64::from(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn write_test_wav(path: &Path) {
        let sample_rate = 8000u32;
        let samples: Vec<i16> = (0..800).map(|i| if i % 2 == 0 { 8000 } else { -8000 }).collect();
        let data_len = (samples.len() * 2) as u32;
        let mut file = File::create(path).unwrap();
        file.write_all(b"RIFF").unwrap();
        file.write_all(&(36 + data_len).to_le_bytes()).unwrap();
        file.write_all(b"WAVE").unwrap();
        file.write_all(b"fmt ").unwrap();
        file.write_all(&16u32.to_le_bytes()).unwrap();
        file.write_all(&1u16.to_le_bytes()).unwrap(); // PCM
        file.write_all(&1u16.to_le_bytes()).unwrap(); // mono
        file.write_all(&sample_rate.to_le_bytes()).unwrap();
        file.write_all(&(sample_rate * 2).to_le_bytes()).unwrap();
        file.write_all(&2u16.to_le_bytes()).unwrap();
        file.write_all(&16u16.to_le_bytes()).unwrap();
        file.write_all(b"data").unwrap();
        file.write_all(&data_len.to_le_bytes()).unwrap();
        for sample in samples {
            file.write_all(&sample.to_le_bytes()).unwrap();
        }
    }

    #[test]
    fn parses_pcm_wav() {
        let path = std::env::temp_dir().join(format!("nlvs-wave-{}.wav", std::process::id()));
        write_test_wav(&path);
        let waveform = read_waveform(&path, 128).unwrap();
        assert!(waveform.supported);
        assert_eq!(waveform.sample_rate, 8000);
        assert_eq!(waveform.channels, 1);
        assert_eq!(waveform.frames, 800);
        assert!(waveform.peaks.max.iter().any(|value| *value > 0.2));
        assert!(waveform.peaks.min.iter().any(|value| *value < -0.2));
        assert_eq!(waveform.rms.len(), waveform.peaks.min.len());
        assert!(waveform.rms.iter().any(|value| *value > 0.1));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn rejects_non_wav() {
        let path = std::env::temp_dir().join(format!("nlvs-wave-bad-{}.bin", std::process::id()));
        std::fs::write(&path, vec![0u8; 64]).unwrap();
        let waveform = read_waveform(&path, 64).unwrap();
        assert!(!waveform.supported);
        let _ = std::fs::remove_file(path);
    }

    /// Scrive un WAV PCM 24-bit stereo con ampiezza nota a tratti (3 s, 48 kHz).
    /// Segmenti: 0–1 s amp 0.5 · 1–2 s amp 0.1 · 2–3 s amp 0.5.
    /// La durata supera più volte il buffer di lettura (256 KiB) per verificare
    /// che i frame a cavallo dei read() non vengano disallineati.
    fn write_test_wav_24bit(path: &Path) {
        let sample_rate = 48_000u32;
        let channels = 2u16;
        let frames = sample_rate as usize * 3;
        let bytes_per_sample = 3usize;
        let block_align = channels as usize * bytes_per_sample;
        let data_len = (frames * block_align) as u32;
        let byte_rate = sample_rate * block_align as u32;
        let mut file = File::create(path).unwrap();
        file.write_all(b"RIFF").unwrap();
        file.write_all(&(36 + data_len).to_le_bytes()).unwrap();
        file.write_all(b"WAVE").unwrap();
        file.write_all(b"fmt ").unwrap();
        file.write_all(&16u32.to_le_bytes()).unwrap();
        file.write_all(&1u16.to_le_bytes()).unwrap(); // PCM
        file.write_all(&channels.to_le_bytes()).unwrap();
        file.write_all(&sample_rate.to_le_bytes()).unwrap();
        file.write_all(&byte_rate.to_le_bytes()).unwrap();
        file.write_all(&(block_align as u16).to_le_bytes()).unwrap();
        file.write_all(&24u16.to_le_bytes()).unwrap();
        file.write_all(b"data").unwrap();
        file.write_all(&data_len.to_le_bytes()).unwrap();
        for frame in 0..frames {
            let amplitude = match frame {
                0..=47_999 => 0.5f64,
                48_000..=95_999 => 0.1f64,
                _ => 0.5f64,
            };
            let phase = 2.0 * std::f64::consts::PI * 440.0 * frame as f64 / f64::from(sample_rate);
            let value = (amplitude * phase.sin() * 8_388_607.0).round() as i32;
            let value = value.clamp(-8_388_608, 8_388_607);
            let bytes = [
                (value & 0xFF) as u8,
                ((value >> 8) & 0xFF) as u8,
                ((value >> 16) & 0xFF) as u8,
            ];
            for _ in 0..channels {
                file.write_all(&bytes).unwrap();
            }
        }
    }

    #[test]
    fn parses_pcm24_stereo_with_known_envelope() {
        let path = std::env::temp_dir().join(format!("nlvs-wave24-{}.wav", std::process::id()));
        write_test_wav_24bit(&path);
        let waveform = read_waveform(&path, 96).unwrap();
        assert!(waveform.supported, "24-bit PCM deve essere supportato");
        assert_eq!(waveform.channels, 2);
        assert_eq!(waveform.bits_per_sample, 24);
        assert_eq!(waveform.sample_rate, 48_000);
        assert_eq!(waveform.frames, 144_000);
        assert!((waveform.duration_sec - 3.0).abs() < 1e-9);
        assert_eq!(waveform.rms.len(), waveform.peaks.min.len());
        assert_eq!(waveform.rms.len(), 96);

        // RMS di una sinusoide di ampiezza A è A/sqrt(2). 96 bucket = 32 per secondo.
        let high = waveform.rms[16];
        let low = waveform.rms[48];
        let late_high = waveform.rms[80];
        assert!(
            (high - std::f32::consts::FRAC_1_SQRT_2 * 0.5).abs() < 0.03,
            "RMS segmento forte: {high}"
        );
        assert!(
            (low - std::f32::consts::FRAC_1_SQRT_2 * 0.1).abs() < 0.02,
            "RMS segmento debole: {low}"
        );
        assert!(
            (late_high - std::f32::consts::FRAC_1_SQRT_2 * 0.5).abs() < 0.03,
            "RMS dopo i confini del buffer: {late_high}"
        );

        // Picchi: ±0.5 / ±0.1 entro la quantizzazione 24-bit, anche a 2–3 s.
        assert!(waveform.peaks.max[16] > 0.45 && waveform.peaks.max[16] < 0.51);
        assert!(waveform.peaks.min[16] < -0.45 && waveform.peaks.min[16] > -0.51);
        assert!(waveform.peaks.max[48] > 0.08 && waveform.peaks.max[48] < 0.11);
        assert!(waveform.peaks.max[80] > 0.45 && waveform.peaks.max[80] < 0.51);
        let _ = std::fs::remove_file(path);
    }

    /// Profilo RMS per secondo sul file reale indicato da `NLVS_TEST_WAV`.
    /// Test diagnostico: stampa i valori per confronto con una misura indipendente.
    #[test]
    fn real_file_rms_profile() {
        let Some(path) = std::env::var_os("NLVS_TEST_WAV") else {
            eprintln!("SKIP real_file_rms_profile: NLVS_TEST_WAV non impostato");
            return;
        };
        let waveform = read_waveform(Path::new(&path), 4096).unwrap();
        assert!(waveform.supported, "file di test non leggibile come WAV PCM");
        println!(
            "REAL duration={:.6} frames={} sr={} ch={} bits={}",
            waveform.duration_sec,
            waveform.frames,
            waveform.sample_rate,
            waveform.channels,
            waveform.bits_per_sample
        );
        // Controllo di allineamento sui primi campioni grezzi (24-bit stereo).
        {
            use std::io::{Read as _, Seek as _, SeekFrom};
            let mut file = File::open(&path).unwrap();
            file.seek(SeekFrom::Start(44)).unwrap();
            let mut raw = [0u8; 12];
            file.read_exact(&mut raw).unwrap();
            let fmt = Fmt {
                format_tag: 1,
                channels: 2,
                sample_rate: 48_000,
                block_align: 6,
                bits_per_sample: 24,
            };
            let left = sample_at(&raw[0..3], fmt);
            let right = sample_at(&raw[3..6], fmt);
            println!("REAL raw frame 0: L={left:.8} R={right:.8}");
        }
        let buckets = waveform.peaks.min.len();
        let duration = waveform.duration_sec.max(0.001);
        for second in 0..duration.ceil() as usize {
            let from = ((second as f64 / duration) * buckets as f64) as usize;
            let to = (((second + 1) as f64 / duration) * buckets as f64)
                .min(buckets as f64) as usize;
            let from = from.min(buckets.saturating_sub(1));
            let to = to.max(from + 1).min(buckets);
            let slice = &waveform.rms[from..to];
            let mean_square =
                slice.iter().map(|value| f64::from(*value).powi(2)).sum::<f64>() / slice.len() as f64;
            let rms = mean_square.sqrt();
            let db = 20.0 * rms.max(1e-9).log10();
            let peak = waveform.peaks.max[from..to]
                .iter()
                .fold(0.0f32, |acc, value| acc.max(value.abs()));
            println!(
                "REAL sec={second:02} rms={rms:.6} db={db:.2} peak={peak:.4}"
            );
        }
    }
}
