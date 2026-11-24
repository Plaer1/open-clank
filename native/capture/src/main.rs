//! One-shot XDG Screenshot portal helper. No input, stream, or retained target.
mod screencast;
mod control;
mod a11y;
#[cfg(feature = "frames")]
mod frame;
use std::{io::{self, Read, Write, Cursor}, time::{Duration, SystemTime, UNIX_EPOCH}};
use serde::{Deserialize, Serialize};
use image::{GenericImageView, ImageFormat, ImageReader, Limits, imageops::FilterType};

const MAX_REQUEST: u64 = 8192;
const MAX_IMAGE: u64 = 64 * 1024 * 1024;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request { version: u8, action: String, owner: String, target: String, target_generation: String, deadline_ms: u64, crop: Option<[u32; 4]>, scale: f64 }
#[derive(Serialize)]
struct Reply<'a> { version: u8, state: &'a str, error: Option<&'a str>, backend: &'a str, target_generation: &'a str, source_time_ms: Option<u64>, source_size: Option<[u32; 2]>, crop: Option<[u32; 4]>, output_size: Option<[u32; 2]>, stream: bool, retained_target: bool, png_bytes: usize }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis().min(u64::MAX as u128) as u64 }
fn reply<'a>(state: &'a str, error: Option<&'a str>, generation: &'a str) -> Reply<'a> { Reply { version: 1, state, error, backend: "xdg-desktop-portal-screenshot/1", target_generation: generation, source_time_ms: None, source_size: None, crop: None, output_size: None, stream: false, retained_target: false, png_bytes: 0 } }
fn valid(req: &Request) -> bool { req.version == 1 && !req.owner.is_empty() && req.owner.len() <= 256 && !req.target_generation.is_empty() && req.target_generation.len() <= 256 && req.target == "portal" && req.scale.is_finite() && (0.05..=1.0).contains(&req.scale) && req.deadline_ms > now() && req.deadline_ms <= now().saturating_add(120_000) }
async fn capture(req: &Request) -> Result<(Vec<u8>, [u32;2], [u32;4], [u32;2], u64), &'static str> {
    let remaining = Duration::from_millis(req.deadline_ms.saturating_sub(now()));
    let shot = tokio::time::timeout(remaining, async {
        ashpd::desktop::screenshot::Screenshot::request().interactive(true).modal(true).send().await
    }).await.map_err(|_| "timeout")?.map_err(|_| "portal_unavailable")?;
    let response = shot.response().map_err(|_| "cancelled_or_denied")?;
    let source_time = now();
    let uri = url::Url::parse(response.uri().as_str()).map_err(|_| "invalid_portal_uri")?;
    let path = uri.to_file_path().map_err(|_| "invalid_portal_uri")?;
    let file = std::fs::File::open(path).map_err(|_| "image_unavailable")?;
    if file.metadata().map_err(|_| "image_unavailable")?.len() > MAX_IMAGE { return Err("size_limit"); }
    let mut reader = ImageReader::with_format(io::BufReader::new(file), ImageFormat::Png);
    let mut limits = Limits::default(); limits.max_image_width = Some(16384); limits.max_image_height = Some(16384); limits.max_alloc = Some(256 * 1024 * 1024); reader.limits(limits);
    let image = reader.decode().map_err(|_| "invalid_image")?;
    let (w,h) = image.dimensions();
    let crop = req.crop.unwrap_or([0,0,w,h]);
    if crop[2] == 0 || crop[3] == 0 || crop[0].checked_add(crop[2]).is_none_or(|x| x > w) || crop[1].checked_add(crop[3]).is_none_or(|y| y > h) { return Err("invalid_crop"); }
    let out = [((crop[2] as f64 * req.scale).round() as u32).max(1), ((crop[3] as f64 * req.scale).round() as u32).max(1)];
    let transformed = image.crop_imm(crop[0], crop[1], crop[2], crop[3]).resize_exact(out[0], out[1], FilterType::Triangle);
    let mut bytes = Cursor::new(Vec::new()); transformed.write_to(&mut bytes, ImageFormat::Png).map_err(|_| "encode_failed")?;
    if bytes.get_ref().len() as u64 > MAX_IMAGE { return Err("size_limit"); }
    if now() >= req.deadline_ms { return Err("timeout"); }
    Ok((bytes.into_inner(), [w,h], crop, out, source_time))
}
#[tokio::main(flavor = "current_thread")]
async fn main() {
    if std::env::args().nth(1).as_deref() == Some("--session-v2") { screencast::run().await; return; }
    if std::env::args().nth(1).as_deref() == Some("--control-v1") { control::run().await; return; }
    if std::env::args().nth(1).as_deref() == Some("--a11y-v1") { a11y::real::run().await; return; }
    let mut input = Vec::new(); let _ = io::stdin().take(MAX_REQUEST + 1).read_to_end(&mut input);
    let parsed = if input.len() as u64 <= MAX_REQUEST { serde_json::from_slice::<Request>(&input).ok() } else { None };
    let mut image = Vec::new();
    let result = if let Some(ref req) = parsed {
        if !valid(req) { reply("error", Some("invalid_request"), "") }
        else if req.action == "readiness" { let mut r = reply("unverified", Some("portal_not_probed"), &req.target_generation); if std::env::var("XDG_SESSION_TYPE").ok().as_deref() != Some("wayland") || std::env::var_os("DBUS_SESSION_BUS_ADDRESS").is_none() { r.state = "unavailable"; r.error = Some("no_wayland_session"); } r }
        else if req.action == "screenshot" {
            if std::env::var("XDG_SESSION_TYPE").ok().as_deref() != Some("wayland") || std::env::var_os("DBUS_SESSION_BUS_ADDRESS").is_none() { reply("error", Some("no_wayland_session"), &req.target_generation) } else { match capture(req).await { Ok((bytes, source, crop, output, at)) => { image = bytes; let mut r = reply("captured", None, &req.target_generation); r.source_time_ms = Some(at); r.source_size = Some(source); r.crop = Some(crop); r.output_size = Some(output); r.png_bytes = image.len(); r }, Err(e) => reply("error", Some(e), &req.target_generation) } }
        } else { reply("error", Some("unsupported_action"), &req.target_generation) }
    } else { reply("error", Some("invalid_request"), "") };
    let mut out = io::stdout().lock(); let _ = serde_json::to_writer(&mut out, &result); let _ = out.write_all(b"\n"); let _ = out.write_all(&image);
}
