use anyhow::{Context, Result};
use clap::Parser;
use chrono::{DateTime, Utc};
use serde::Serialize;
use serialport::SerialPort;
use std::thread;
use std::time::{Duration, Instant};

/// Simple Modbus RTU reader for common RS485 Soil NPK sensors
/// Defaults use many off-the-shelf sensors: FC=0x03, unit=1, baud=9600, regs 0x001E..0x0020
#[derive(Parser, Debug)]
#[command(name = "npk-reader", about = "Read N/P/K from RS485/USB soil NPK Modbus-RTU sensor")]
struct Opts {
    /// Serial port path (e.g., /dev/ttyUSB0 on Linux, COM5 on Windows)
    #[arg(short, long, default_value = "/dev/ttyUSB0")] 
    port: String,

    /// Baud rate (most sensors use 9600)
    #[arg(short = 'b', long, default_value_t = 9600)]
    baud: u32,

    /// Modbus slave/unit ID
    #[arg(short, long, default_value_t = 1)]
    unit: u8,

    /// Holding register address for Nitrogen (N)
    #[arg(long = "n-reg", default_value_t = 0x001E)]
    n_reg: u16,

    /// Holding register address for Phosphorus (P)
    #[arg(long = "p-reg", default_value_t = 0x001F)]
    p_reg: u16,

    /// Holding register address for Potassium (K)
    #[arg(long = "k-reg", default_value_t = 0x0020)]
    k_reg: u16,

    /// Number of registers per value (1 = u16, 2 = u32/float)
    #[arg(long, default_value_t = 1)]
    reg_width: u16,

    /// Swap 32-bit word order if reg_width=2 (some sensors need this)
    #[arg(long, default_value_t = false)]
    swap_words: bool,

    /// Read interval in seconds (0 = single read)
    #[arg(short, long, default_value_t = 0)]
    interval: u64,

    /// Per-request timeout (ms)
    #[arg(long, default_value_t = 1500)]
    timeout_ms: u64,

    /// Retries if CRC/timeout error
    #[arg(long, default_value_t = 2)]
    retries: u8,
}

#[derive(Debug, Serialize)]
struct NpkReading {
    ts: DateTime<Utc>,
    unit: u8,
    n_mgkg: f32,
    p_mgkg: f32,
    k_mgkg: f32,
    raw: serde_json::Value,
}

fn crc16_modbus(data: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    for &b in data {
        crc ^= b as u16;
        for _ in 0..8 {
            let lsb = crc & 0x0001;
            crc >>= 1;
            if lsb != 0 { crc ^= 0xA001; }
        }
    }
    crc
}

fn build_read_holding(unit: u8, start: u16, count: u16) -> Vec<u8> {
    let mut frame = vec![unit, 0x03, (start >> 8) as u8, (start & 0xFF) as u8, (count >> 8) as u8, (count & 0xFF) as u8];
    let crc = crc16_modbus(&frame);
    frame.push((crc & 0xFF) as u8);      // CRC low
    frame.push((crc >> 8) as u8);        // CRC high
    frame
}

fn write_and_read(port: &mut dyn SerialPort, req: &[u8], timeout: Duration) -> Result<Vec<u8>> {
    // Flush any stale bytes
    let _ = port.clear(serialport::ClearBuffer::All);

    port.write_all(req).context("write request")?;
    port.flush().ok();

    let start = Instant::now();
    let mut buf = Vec::with_capacity(256);

    // First read enough to know expected length
    while start.elapsed() < timeout {
        let mut tmp = [0u8; 64];
        match port.read(&mut tmp) {
            Ok(n) if n > 0 => {
                buf.extend_from_slice(&tmp[..n]);
                // Need at least 5 bytes to determine len: unit, func, byte_count, data..., crc_lo, crc_hi
                if buf.len() >= 5 {
                    // Check basic header
                    if buf[1] == 0x83 { // exception response
                        anyhow::bail!("Modbus exception code 0x{:02X}", buf.get(2).copied().unwrap_or(0));
                    }
                    if buf[1] != 0x03 { /* keep reading; some sensors echo */ }
                    let byte_count = buf[2] as usize;
                    let expected = 3 + byte_count + 2; // header + data + CRC
                    if buf.len() >= expected {
                        // Validate CRC
                        let (payload, crc_bytes) = buf.split_at(expected - 2);
                        let rx_crc = u16::from_le_bytes([crc_bytes[0], crc_bytes[1]]);
                        let calc_crc = crc16_modbus(payload);
                        if rx_crc != calc_crc {
                            anyhow::bail!("CRC mismatch: rx=0x{rx_crc:04X} calc=0x{calc_crc:04X}");
                        }
                        return Ok(payload.to_vec());
                    }
                }
            }
            Ok(_) => { /* no data yet */ }
            Err(ref e) if e.kind() == std::io::ErrorKind::TimedOut => { /* spin */ }
            Err(e) => return Err(e).context("serial read error"),
        }
        thread::sleep(Duration::from_millis(10));
    }

    anyhow::bail!("timeout waiting for response")
}

fn read_registers_u16(port: &mut dyn SerialPort, unit: u8, addr: u16, count: u16, timeout: Duration, retries: u8) -> Result<Vec<u16>> {
    let req = build_read_holding(unit, addr, count);
    let mut last_err: Option<anyhow::Error> = None;
    for _ in 0..=retries {
        match write_and_read(port, &req, timeout) {
            Ok(payload) => {
                let byte_count = payload[2] as usize;
                if byte_count % 2 != 0 { anyhow::bail!("invalid byte count {byte_count}"); }
                let mut out = Vec::with_capacity(byte_count / 2);
                for i in (3..3 + byte_count).step_by(2) {
                    let hi = payload[i] as u16;
                    let lo = payload[i + 1] as u16;
                    out.push((hi << 8) | lo);
                }
                return Ok(out);
            }
            Err(e) => {
                last_err = Some(e);
                thread::sleep(Duration::from_millis(100));
            }
        }
    }
    Err(last_err.unwrap_or_else(|| anyhow::anyhow!("unknown error")))
}

fn as_value(words: &[u16], swap_words: bool) -> f32 {
    match words.len() {
        0 => f32::NAN,
        1 => words[0] as f32,
        2 => {
            let (w0, w1) = if swap_words { (words[1], words[0]) } else { (words[0], words[1]) };
            let u = ((w0 as u32) << 16) | (w1 as u32);
            // Try interpreting as IEEE754 float; if it's not a sane float, also return as integer
            let f = f32::from_bits(u);
            if f.is_finite() { f } else { u as f32 }
        }
        _ => f32::NAN,
    }
}

fn main() -> Result<()> {
    let opts = Opts::parse();

    let mut port = serialport::new(&opts.port, opts.baud)
        .timeout(Duration::from_millis(opts.timeout_ms))
        .parity(serialport::Parity::None)
        .data_bits(serialport::DataBits::Eight)
        .stop_bits(serialport::StopBits::One)
        .open()
        .with_context(|| format!("Failed to open serial port {}", opts.port))?;

    let poll = opts.interval;

    loop {
        let timeout = Duration::from_millis(opts.timeout_ms);

        let n_words = read_registers_u16(&mut *port, opts.unit, opts.n_reg, opts.reg_width, timeout, opts.retries)?;
        let p_words = read_registers_u16(&mut *port, opts.unit, opts.p_reg, opts.reg_width, timeout, opts.retries)?;
        let k_words = read_registers_u16(&mut *port, opts.unit, opts.k_reg, opts.reg_width, timeout, opts.retries)?;

        let n = as_value(&n_words, opts.swap_words);
        let p = as_value(&p_words, opts.swap_words);
        let k = as_value(&k_words, opts.swap_words);

        let reading = NpkReading {
            ts: Utc::now(),
            unit: opts.unit,
            n_mgkg: n,
            p_mgkg: p,
            k_mgkg: k,
            raw: serde_json::json!({
                "n_words": n_words,
                "p_words": p_words,
                "k_words": k_words,
                "n_reg": opts.n_reg,
                "p_reg": opts.p_reg,
                "k_reg": opts.k_reg,
                "reg_width": opts.reg_width,
                "swap_words": opts.swap_words,
            }),
        };

        println!("{}", serde_json::to_string_pretty(&reading)?);

        if poll == 0 { break; }
        thread::sleep(Duration::from_secs(poll));
    }

    Ok(())
}

// cargo run --release -- --port COM5 --baud 4800 --unit 1 --n-reg 30 --p-reg 31 --k-reg 32