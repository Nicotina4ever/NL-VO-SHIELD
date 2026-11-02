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

    reader.seek(SeekFrom::Start(offset))?;
    let mut buffer = vec![0u8; 256 * 1024];
    let mut remaining = data_size as usize;
    let mut frame_index: u64 = 0;

    while remaining > 0 {
        let to_read = remaining.min(buffer.len());
        let read = reader.read(&mut buffer[..to_read])?;
        if read == 0 {
            break;
        }
        let mut pos = 0usize;
        while pos + block_align <= read {
            let bucket = ((frame_index as f64 / frames_per_bucket) as usize).min(bucket_count - 1);
            let mut frame_min = 1.0f32;
            let mut frame_max = -1.0f32;
            for channel in 0..usize::from(fmt.channels) {
                let base = pos + channel * bytes_per_sample;
                if base + bytes_per_sample > read {
                    break;
                }
                let value = sample_at(&buffer[base..base + bytes_per_sample], fmt);
                if value < frame_min {
                    frame_min = value;
                }
                if value > frame_max {
                    frame_max = value;
                }
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
        remaining = remaining.saturating_sub(read);
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

    Ok(WaveformData {
        supported: true,
        reason: None,
        sample_rate: fmt.sample_rate,
        channels: fmt.channels,
        bits_per_sample: fmt.bits_per_sample,
        duration_sec: frames as f64 / f64::from(fmt.sample_rate),
        frames,
        peaks: Peaks { min: mins, max: maxs },
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
}
