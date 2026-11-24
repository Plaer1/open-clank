//! Long-lived `--control-v1` helper: JSON Lines on stdin, one JSON object per line on stdout.
//! All session rules live in `session()`; `Real` is the thin ashpd layer behind the `Portal` trait.
//! Nothing typed or clicked is ever logged; replies carry codes only.
use std::{io::{self, Write}, sync::atomic::{AtomicBool, Ordering}, time::Duration};
#[cfg(any(feature = "cursor-overlay", test))]
#[path = "overlay.rs"]
mod overlay;
use serde::Deserialize;
use tokio::{io::{AsyncBufReadExt, AsyncReadExt, BufReader}, sync::mpsc, time::Instant};

const MAX_LINE: u64 = 8192;
const POLL: Duration = Duration::from_millis(100);
const BTN_LEFT: i32 = 0x110;
const BTN_RIGHT: i32 = 0x111;
const BTN_MIDDLE: i32 = 0x112;
const KEY_RETURN: i32 = 0xff0d;
const KEY_TAB: i32 = 0xff09;

#[derive(Clone, Copy)]
pub struct Limits { pub max_actions: u32, pub max_life: Duration, pub min_gap: Duration, pub max_deadline: Duration }
pub const LIMITS: Limits = Limits { max_actions: 20, max_life: Duration::from_secs(120), min_gap: Duration::from_millis(50), max_deadline: Duration::from_secs(120) };

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Frame { width: u32, height: u32 }
#[derive(Deserialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
enum Btn { Left, Right, Middle }
#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "lowercase", deny_unknown_fields)]
enum Req {
    Begin { v: u8, deadline_ms: u64, #[serde(default)] stream_handoff: Option<StreamHandoff>, #[serde(default)] clipboard: Option<String> },
    FrameHandoff { v: u8, handoff: StreamHandoff },
    Click { v: u8, x: i64, y: i64, button: Btn, frame: Frame },
    /// Pointer move only: no button is pressed.
    Move { v: u8, x: i64, y: i64, frame: Frame },
    Key { v: u8, keysyms: Vec<u32> },
    Type { v: u8, text: String },
    Scroll { v: u8, x: i64, y: i64, direction: Dir, amount: i64, frame: Frame },
    Drag { v: u8, x1: i64, y1: i64, x2: i64, y2: i64, frame: Frame },
    Paste { v: u8, text: String, #[serde(default)] clipboard: Option<String> },
    /// First request only: ask the portal what it offers, without opening a session or any dialog.
    Probe { v: u8 },
    Stop { v: u8 },
}
#[derive(Deserialize, Clone, Copy)]
#[serde(rename_all = "lowercase")]
enum Dir { Up, Down, Left, Right }
impl Req { fn v(&self) -> u8 { match self { Req::Begin { v, .. } | Req::FrameHandoff { v, .. } | Req::Click { v, .. } | Req::Move { v, .. } | Req::Key { v, .. } | Req::Type { v, .. } | Req::Scroll { v, .. } | Req::Drag { v, .. } | Req::Paste { v, .. } | Req::Probe { v } | Req::Stop { v } => *v } } }
const DRAG_STEPS: i64 = 12;
const MAX_SCROLL: i64 = 20;

/// What the portal must be asked for. Constant; the fake records it so tests can pin it.
#[derive(Clone, Debug, PartialEq)]
pub struct BeginParams { pub keyboard: bool, pub pointer: bool, pub monitor_only: bool, pub multiple: bool, pub cursor_embedded: bool, pub persist_do_not: bool, pub restore_token: Option<String>, pub clipboard: bool }
pub const BEGIN: BeginParams = BeginParams { keyboard: true, pointer: true, monitor_only: true, multiple: false, cursor_embedded: true, persist_do_not: true, restore_token: None, clipboard: false };
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct StreamInfo { pub node: u32, pub width: u32, pub height: u32 }
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum BeginErr { Denied, Unavailable, Unsupported }
#[derive(Clone, Copy, Debug, PartialEq)]
enum Held { Button(i32), Key(i32) }

pub trait Portal {
    fn ready(&self) -> bool;
    fn stream_handoff(&mut self, _handoff: Option<StreamHandoff>) {}
    async fn begin(&mut self, p: &BeginParams) -> Result<StreamInfo, BeginErr>;
    async fn frame_handoff(&mut self, _handoff: StreamHandoff) -> Result<(), ()> { Err(()) }
    async fn motion_absolute(&mut self, node: u32, x: f64, y: f64) -> Result<(), ()>;
    async fn button(&mut self, code: i32, down: bool) -> Result<(), ()>;
    async fn keysym(&mut self, sym: i32, down: bool) -> Result<(), ()>;
    /// Discrete wheel steps on one axis (positive = down/right).
    async fn axis(&mut self, vertical: bool, steps: i32) -> Result<(), ()>;
    fn clipboard_enabled(&self) -> bool { false }
    fn clipboard_current(&self) -> bool { false }
    async fn clipboard_replace(&mut self, _text: &str) -> Result<(), ()> { Err(()) }
    /// Portal version and the device types it offers, read without a session or a dialog.
    async fn probe(&mut self) -> Result<(u32, bool, bool), BeginErr>;
    /// The portal ended the session (the human revoked it from the system indicator): stop everything.
    fn gone(&self) -> bool { false }
    fn shortcut_description(&self) -> Option<&str> { None }
    fn overlay_at_begin(&self) -> Option<bool> { None }
    fn recording_target(&self) -> Option<serde_json::Value> { None }
    async fn close(&mut self);
}

fn reply(ok: bool, code: &str) -> String { if ok { format!("{{\"ok\":true,\"state\":\"{code}\"}}") } else { format!("{{\"ok\":false,\"code\":\"{code}\"}}") } }

enum Ev { Line(Vec<u8>), End, Deadline, Takeover }
async fn next(rx: &mut mpsc::Receiver<Option<Vec<u8>>>, stop: &AtomicBool, until: Option<Instant>, gone: impl Fn() -> bool) -> Ev {
    loop {
        if stop.load(Ordering::Relaxed) { return Ev::End; }
        if gone() { return Ev::Takeover; }
        if until.is_some_and(|t| Instant::now() >= t) { return Ev::Deadline; }
        tokio::select! { x = rx.recv() => return match x { Some(Some(l)) => Ev::Line(l), _ => Ev::End }, _ = tokio::time::sleep(POLL) => {} }
    }
}

/// Release everything held (reverse order), then close the session once. True if all releases worked.
async fn shut<P: Portal>(p: &mut P, held: &mut Vec<Held>) -> bool {
    let mut ok = true;
    while let Some(h) = held.pop() {
        let r = match h { Held::Button(c) => p.button(c, false).await, Held::Key(k) => p.keysym(k, false).await };
        if r.is_err() { ok = false; }
    }
    let _ = tokio::time::timeout(Duration::from_secs(1), p.close()).await;
    ok
}
/// Release only (session stays). True if all releases worked.
async fn release_all<P: Portal>(p: &mut P, held: &mut Vec<Held>) -> bool {
    let mut failed = Vec::new();
    while let Some(h) = held.pop() {
        let r = match h { Held::Button(c) => p.button(c, false).await, Held::Key(k) => p.keysym(k, false).await };
        if r.is_err() { failed.push(h); }
    }
    // Keep failed releases tracked so shutdown gets one final chance before closing.
    held.extend(failed.into_iter().rev());
    held.is_empty()
}
async fn press<P: Portal>(p: &mut P, held: &mut Vec<Held>, h: Held) -> Result<(), ()> {
    held.push(h); // tracked before the call: a failed press may still have been delivered
    match h { Held::Button(c) => p.button(c, true).await, Held::Key(k) => p.keysym(k, true).await }
}
async fn release<P: Portal>(p: &mut P, held: &mut Vec<Held>, h: Held) -> Result<(), ()> {
    match h { Held::Button(c) => p.button(c, false).await, Held::Key(k) => p.keysym(k, false).await }?;
    if let Some(i) = held.iter().rposition(|x| *x == h) { held.remove(i); }
    Ok(())
}
fn keysym_of(c: char) -> i32 {
    match c { '\n' => KEY_RETURN, '\t' => KEY_TAB, ' '..='~' | '\u{a0}'..='\u{ff}' => c as i32, _ => 0x0100_0000 + c as i32 }
}
fn text_ok(t: &str) -> bool { let n = t.chars().count(); (1..=256).contains(&n) && t.chars().all(|c| !c.is_control() || c == '\n' || c == '\t') }

pub async fn session<P: Portal>(p: &mut P, rx: &mut mpsc::Receiver<Option<Vec<u8>>>, out: &mut impl FnMut(String) -> bool, stop: &AtomicBool, takeover: &AtomicBool, lim: Limits) {
    // First request must be a valid begin.
    let Ev::Line(first) = next(rx, stop, None, || false).await else { return };
    let mut begin_params = BEGIN.clone();
    let deadline = match serde_json::from_slice::<Req>(&first) {
        Err(_) => { out(reply(false, "invalid_request")); return }
        Ok(r) if r.v() != 1 => { out(reply(false, "invalid_request")); return }
        Ok(Req::Begin { deadline_ms, stream_handoff, clipboard, .. }) if (1..).contains(&deadline_ms) && matches!(clipboard.as_deref(), None | Some("replace")) => { begin_params.clipboard = clipboard.is_some(); p.stream_handoff(stream_handoff); Duration::from_millis(deadline_ms).min(lim.max_deadline) },
        Ok(Req::Begin { .. }) => { out(reply(false, "invalid_request")); return }
        Ok(Req::Probe { .. }) => {
            if !p.ready() { out(reply(false, "no_wayland_session")); return }
            out(match tokio::time::timeout(Duration::from_secs(5), p.probe()).await {
                Ok(Ok((version, keyboard, pointer))) => format!("{{\"ok\":true,\"state\":\"probe\",\"version\":{version},\"keyboard\":{keyboard},\"pointer\":{pointer}}}"),
                Ok(Err(_)) => reply(false, "unavailable"), Err(_) => reply(false, "timeout"),
            });
            return
        }
        Ok(_) => { out(reply(false, "not_begun")); return }
    };
    if !p.ready() { out(reply(false, "no_wayland_session")); return }
    let started = {
        let begin = p.begin(&begin_params);
        tokio::pin!(begin);
        let end = Instant::now() + deadline;
        loop {
            tokio::select! {
                r = &mut begin => break Some(r),
                _ = tokio::time::sleep(POLL) => { if stop.load(Ordering::Relaxed) || Instant::now() >= end { break None } }
            }
        }
    };
    let stream = match started {
        None => { let _ = tokio::time::timeout(Duration::from_secs(1), p.close()).await; out(reply(false, "timeout")); return }
        Some(Err(e)) => {
            let _ = tokio::time::timeout(Duration::from_secs(1), p.close()).await;
            out(reply(false, match e { BeginErr::Denied => "denied", BeginErr::Unavailable => "unavailable", BeginErr::Unsupported => "unsupported_target" }));
            return
        }
        Some(Ok(s)) => s,
    };
    let end = Instant::now() + lim.max_life.min(deadline);
    let mut held: Vec<Held> = Vec::new();
    let shortcut = p.shortcut_description().map(|d| format!(",\"takeover_shortcut\":{}", serde_json::json!(d))).unwrap_or_default();
    let overlay = p.overlay_at_begin().map(|v| format!(",\"cursor_overlay_at_begin\":{v}")).unwrap_or_default();
    let target = p.recording_target().map(|v| format!(",\"control_target\":{v}")).unwrap_or_default();
    let clipboard = if begin_params.clipboard { format!(",\"clipboard_enabled\":{}", p.clipboard_enabled()) } else { String::new() };
    let active = format!("{{\"ok\":true,\"state\":\"active\",\"stream\":{{\"width\":{},\"height\":{}}}{}{}{}{}}}", stream.width, stream.height, clipboard, shortcut, overlay, target);
    if !out(active) { shut(p, &mut held).await; return }
    let (mut used, mut last): (u32, Option<Instant>) = (0, None);
    loop {
        if p.gone() { takeover.store(true, Ordering::Relaxed); shut(p, &mut held).await; out(reply(false, "taken_over")); return }
        let line = match next(rx, stop, Some(end), || p.gone()).await {
            Ev::Line(l) => l,
            Ev::End => { shut(p, &mut held).await; return }
            Ev::Takeover => { takeover.store(true, Ordering::Relaxed); shut(p, &mut held).await; out(reply(false, "taken_over")); return }
            Ev::Deadline => { shut(p, &mut held).await; out(reply(false, "expired")); return }
        };
        if p.gone() { takeover.store(true, Ordering::Relaxed); shut(p, &mut held).await; out(reply(false, "taken_over")); return }
        let req = match serde_json::from_slice::<Req>(&line) { Ok(r) if r.v() == 1 => r, _ => { if !out(reply(false, "invalid_request")) { shut(p, &mut held).await; return } continue } };
        // Validate first: refusals make zero portal calls and use no budget.
        let refuse = match &req {
            Req::Begin { .. } | Req::Probe { .. } => Some("invalid_request"),
            Req::Stop { .. } | Req::FrameHandoff { .. } => None,
            Req::Click { x, y, frame, .. } => if frame.width != stream.width || frame.height != stream.height { Some("frame_mismatch") } else if *x < 0 || *y < 0 || *x >= stream.width as i64 || *y >= stream.height as i64 { Some("out_of_bounds") } else { None },
            Req::Move { x, y, frame, .. } => if frame.width != stream.width || frame.height != stream.height { Some("frame_mismatch") } else if *x < 0 || *y < 0 || *x >= stream.width as i64 || *y >= stream.height as i64 { Some("out_of_bounds") } else { None },
            Req::Key { keysyms, .. } => if (1..=4).contains(&keysyms.len()) && keysyms.iter().all(|k| (1..=0x1fff_ffff).contains(k)) { None } else { Some("invalid_request") },
            Req::Type { text, .. } => if text_ok(text) { None } else { Some("invalid_text") },
            Req::Paste { text, clipboard, .. } => if !text_ok(text) { Some("invalid_text") } else if clipboard.as_deref() != Some("replace") || !p.clipboard_enabled() { Some("unsupported_clipboard") } else { None },
            Req::Scroll { x, y, amount, frame, .. } => if frame.width != stream.width || frame.height != stream.height { Some("frame_mismatch") }
                else if *x < 0 || *y < 0 || *x >= stream.width as i64 || *y >= stream.height as i64 { Some("out_of_bounds") }
                else if !(1..=MAX_SCROLL).contains(amount) { Some("invalid_request") } else { None },
            Req::Drag { x1, y1, x2, y2, frame, .. } => if frame.width != stream.width || frame.height != stream.height { Some("frame_mismatch") }
                else if [x1, x2].iter().any(|x| **x < 0 || **x >= stream.width as i64) || [y1, y2].iter().any(|y| **y < 0 || **y >= stream.height as i64) { Some("out_of_bounds") } else { None },
        };
        if let Some(code) = refuse { if !out(reply(false, code)) { shut(p, &mut held).await; return } continue }
        if let Req::Stop { .. } = req { shut(p, &mut held).await; out(reply(true, "stopped")); return }
        if let Req::FrameHandoff { handoff, .. } = &req {
            let ok = !p.gone() && matches!(tokio::time::timeout(Duration::from_secs(5), p.frame_handoff(handoff.clone())).await, Ok(Ok(()))) && !p.gone();
            if !out(reply(ok, if ok { "frame_handoff" } else { "unavailable" })) { shut(p, &mut held).await; return }
            continue;
        }
        if used >= lim.max_actions { shut(p, &mut held).await; out(reply(false, "expired")); return }
        if let Some(t) = last { tokio::time::sleep_until(t + lim.min_gap).await; }
        if Instant::now() >= end { shut(p, &mut held).await; out(reply(false, "expired")); return }
        used += 1;
        if let Req::Paste { text, .. } = &req {
            // The user expressly approved replacement; no earlier selection is read or restored.
            if p.gone() { shut(p, &mut held).await; out(reply(false, "taken_over")); return }
            if p.clipboard_replace(text).await.is_err() {
                out(r#"{"ok":false,"clipboard":"unknown","input":"not_issued","lifetime":"session"}"#.into());
                shut(p, &mut held).await; return
            }
            let mut input_issued = false;
            let sent: Result<(), ()> = async {
                for k in [0xffe3, 'v' as i32] {
                    if p.gone() || !p.clipboard_current() { return Err(()); }
                    input_issued = true; // a failed press may still have reached the target
                    press(p, &mut held, Held::Key(k)).await?;
                }
                for k in [0xffe3, 'v' as i32].iter().rev() { release(p, &mut held, Held::Key(*k)).await?; }
                if p.gone() || !p.clipboard_current() { return Err(()); }
                Ok(())
            }.await;
            last = Some(Instant::now());
            if sent.is_err() || p.gone() || !p.clipboard_current() {
                release_all(p, &mut held).await;
                let input = if input_issued { "unknown" } else { "not_issued" };
                out(format!(r#"{{"ok":false,"clipboard":"replaced","input":"{input}","lifetime":"session"}}"#));
                shut(p, &mut held).await; return
            }
            if !out(r#"{"ok":true,"clipboard":"replaced","input":"sent","lifetime":"session"}"#.into()) { shut(p, &mut held).await; return }
            continue
        }
        let result: Result<(), ()> = async {
            macro_rules! active { () => { if p.gone() { return Err(()); } } }
            active!();
            match &req {
                Req::Click { x, y, button, .. } => {
                    let b = Held::Button(match button { Btn::Left => BTN_LEFT, Btn::Right => BTN_RIGHT, Btn::Middle => BTN_MIDDLE });
                    p.motion_absolute(stream.node, *x as f64, *y as f64).await?;
                    active!(); press(p, &mut held, b).await?;
                    release(p, &mut held, b).await
                }
                Req::Move { x, y, .. } => p.motion_absolute(stream.node, *x as f64, *y as f64).await,
                Req::Key { keysyms, .. } => {
                    for k in keysyms { active!(); press(p, &mut held, Held::Key(*k as i32)).await?; }
                    for k in keysyms.iter().rev() { release(p, &mut held, Held::Key(*k as i32)).await?; }
                    Ok(())
                }
                Req::Type { text, .. } => {
                    for c in text.chars() { active!(); let k = Held::Key(keysym_of(c)); press(p, &mut held, k).await?; release(p, &mut held, k).await?; }
                    Ok(())
                }
                Req::Scroll { x, y, direction, amount, .. } => {
                    p.motion_absolute(stream.node, *x as f64, *y as f64).await?;
                    active!();
                    let (vertical, sign) = match direction { Dir::Up => (true, -1), Dir::Down => (true, 1), Dir::Left => (false, -1), Dir::Right => (false, 1) };
                    p.axis(vertical, sign * *amount as i32).await
                }
                Req::Drag { x1, y1, x2, y2, .. } => {
                    let b = Held::Button(BTN_LEFT);
                    p.motion_absolute(stream.node, *x1 as f64, *y1 as f64).await?;
                    active!(); press(p, &mut held, b).await?;
                    for i in 1..=DRAG_STEPS {
                        active!();
                        let (fx, fy) = (*x1 as f64 + (*x2 - *x1) as f64 * i as f64 / DRAG_STEPS as f64, *y1 as f64 + (*y2 - *y1) as f64 * i as f64 / DRAG_STEPS as f64);
                        p.motion_absolute(stream.node, fx, fy).await?;
                    }
                    release(p, &mut held, b).await
                }
                _ => Ok(()),
            }
        }.await;
        last = Some(Instant::now());
        if p.gone() {
            takeover.store(true, Ordering::Relaxed);
            release_all(p, &mut held).await;
            shut(p, &mut held).await;
            out(reply(false, "taken_over"));
            return;
        }
        let sent = match result {
            Ok(()) => out(format!("{{\"ok\":true,\"outcome\":\"sent\",\"actions_left\":{}}}", lim.max_actions - used)),
            Err(()) => {
                // Never retry the action; keep the session only if everything held was released.
                if !release_all(p, &mut held).await { out(reply(false, "outcome_unknown")); shut(p, &mut held).await; return }
                out(reply(false, "outcome_unknown"))
            }
        };
        if !sent { shut(p, &mut held).await; return }
    }
}

// ---- thin ashpd layer: no logic beyond mapping calls and errors ----
use ashpd::desktop::{clipboard::{Clipboard, RequestClipboardOptions, SetSelectionOptions}, global_shortcuts::{GlobalShortcuts, NewShortcut}, PersistMode, Session, remote_desktop::{DeviceType, KeyState, NotifyKeyboardKeysymOptions, NotifyPointerButtonOptions, NotifyPointerMotionAbsoluteOptions, RemoteDesktop, SelectDevicesOptions, Axis, NotifyPointerAxisDiscreteOptions}, screencast::{CursorMode, Screencast, SelectSourcesOptions, SourceType}};
// Clipboard values exist only in the opted-in RemoteDesktop session. They are
// served on request, never recovered from or restored to the user's clipboard.
struct ClipboardOwner {
    portal: std::sync::Arc<Clipboard>,
    session: &'static Session<RemoteDesktop>,
    alive: std::sync::Arc<AtomicBool>,
    owns: std::sync::Arc<AtomicBool>,
    generation: std::sync::Arc<std::sync::atomic::AtomicU64>,
    selected_generation: Option<u64>,
    text: std::sync::Arc<std::sync::Mutex<Option<Vec<u8>>>>,
    worker: tokio::task::JoinHandle<()>,
}
impl ClipboardOwner {
    async fn prepare(connection: &zbus::Connection, session: &'static Session<RemoteDesktop>) -> Result<Self, ()> {
        use futures_util::StreamExt;
        let portal = std::sync::Arc::new(Clipboard::with_connection(connection.clone()).await.map_err(|_| ())?);
        let alive = std::sync::Arc::new(AtomicBool::new(true));
        let owns = std::sync::Arc::new(AtomicBool::new(false));
        let generation = std::sync::Arc::new(std::sync::atomic::AtomicU64::new(0));
        let text = std::sync::Arc::new(std::sync::Mutex::new(None::<Vec<u8>>));
        let expected = serde_json::to_string(session).map_err(|_| ())?;
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        let (portal2, alive2, owns2, generation2, text2) = (portal.clone(), alive.clone(), owns.clone(), generation.clone(), text.clone());
        let worker = tokio::spawn(async move {
            let (Ok(changes), Ok(transfers)) = (portal2.receive_selection_owner_changed::<RemoteDesktop>().await, portal2.receive_selection_transfer::<RemoteDesktop>().await) else { let _ = ready_tx.send(false); return; };
            let mut changes = Box::pin(changes); let mut transfers = Box::pin(transfers);
            let _ = ready_tx.send(true);
            let mut serials = std::collections::HashSet::new();
            let mut transfer_count = 0usize;
            let mut writes = tokio::task::JoinSet::new();
            while alive2.load(Ordering::Acquire) {
                tokio::select! {
                    changed = changes.next() => {
                        let Some((which, event)) = changed else { break };
                        if serde_json::to_string(&which).ok().as_deref() != Some(expected.as_str()) { continue; }
                        if event.session_is_owner() == Some(true) {
                            owns2.store(text2.lock().ok().is_some_and(|v| v.is_some()), Ordering::Release);
                        } else {
                            owns2.store(false, Ordering::Release); generation2.fetch_add(1, Ordering::AcqRel);
                            if let Ok(mut value) = text2.lock() { *value = None; }
                        }
                    }
                    transfer = transfers.next() => {
                        let Some((which, mime, serial)) = transfer else { break };
                        if serde_json::to_string(&which).ok().as_deref() != Some(expected.as_str()) { continue; }
                        if transfer_count >= 64 {
                            let _ = tokio::time::timeout(Duration::from_millis(250), portal2.selection_write_done(session, serial, false)).await;
                            break;
                        }
                        transfer_count += 1;
                        let portal = portal2.clone(); let alive = alive2.clone(); let owns = owns2.clone(); let generation = generation2.clone(); let expected_generation = generation.load(Ordering::Acquire); let text = text2.clone();
                        let unique = clipboard_serial_admit(&mut serials, serial);
                        writes.spawn(async move {
                            let value = if clipboard_transfer_allowed(true, &mime, unique, alive.load(Ordering::Acquire), owns.load(Ordering::Acquire)) { text.lock().ok().and_then(|v| v.clone()) } else { None };
                            let mut ok = false;
                            if let Some(value) = value {
                                if let Ok(Ok(fd)) = tokio::time::timeout(Duration::from_secs(2), portal.selection_write(session, serial)).await {
                                    let fd: std::os::fd::OwnedFd = fd.into();
                                    ok = tokio::task::spawn_blocking(move || clipboard_write(fd, &value, &alive, &owns, &generation, expected_generation)).await.unwrap_or(false);
                                }
                            }
                            let _ = tokio::time::timeout(Duration::from_secs(1), portal.selection_write_done(session, serial, ok)).await;
                        });
                    }
                    _ = writes.join_next(), if !writes.is_empty() => {}
                    _ = tokio::time::sleep(POLL) => {}
                }
            }
            alive2.store(false, Ordering::Release); owns2.store(false, Ordering::Release);
            if let Ok(mut value) = text2.lock() { *value = None; }
            while !writes.is_empty() { if tokio::time::timeout(Duration::from_secs(2), writes.join_next()).await.is_err() { writes.abort_all(); break; } }
        });
        let subscribed = matches!(tokio::time::timeout(Duration::from_secs(2), ready_rx).await, Ok(Ok(true)));
        if !subscribed || !matches!(tokio::time::timeout(Duration::from_secs(2), portal.request(session, RequestClipboardOptions::default())).await, Ok(Ok(()))) {
            alive.store(false, Ordering::Release); worker.abort(); return Err(())
        }
        Ok(Self { portal, session, alive, owns, generation, selected_generation: None, text, worker })
    }
    async fn replace(&mut self, text: &str) -> Result<(), ()> {
        if !self.alive.load(Ordering::Acquire) || !text_ok(text) { return Err(()); }
        self.selected_generation = None;
        self.owns.store(false, Ordering::Release); self.generation.fetch_add(1, Ordering::AcqRel);
        *self.text.lock().map_err(|_| ())? = Some(text.as_bytes().to_vec());
        let types = ["text/plain;charset=utf-8", "text/plain"];
        let generation = self.generation.load(Ordering::Acquire);
        let issued = matches!(tokio::time::timeout(Duration::from_secs(2), self.portal.set_selection(self.session, SetSelectionOptions::default().set_mime_types(&types))).await, Ok(Ok(())));
        if issued {
            let deadline = Instant::now() + Duration::from_secs(1);
            while self.alive.load(Ordering::Acquire) && self.generation.load(Ordering::Acquire) == generation && Instant::now() < deadline {
                if self.owns.load(Ordering::Acquire) { self.selected_generation = Some(generation); return Ok(()); }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
        self.owns.store(false, Ordering::Release); if let Ok(mut value) = self.text.lock() { *value = None; } Err(())
    }
    fn current(&self) -> bool {
        self.alive.load(Ordering::Acquire) && self.owns.load(Ordering::Acquire) &&
            self.selected_generation.is_some_and(|selected| self.generation.load(Ordering::Acquire) == selected)
    }
    async fn close(&mut self) {
        self.alive.store(false, Ordering::Release); self.owns.store(false, Ordering::Release);
        if let Ok(mut value) = self.text.lock() { *value = None; }
        if tokio::time::timeout(Duration::from_secs(3), &mut self.worker).await.is_err() { self.worker.abort(); }
    }
}
impl Drop for ClipboardOwner {
    fn drop(&mut self) {
        self.alive.store(false, Ordering::Release); self.owns.store(false, Ordering::Release);
        if let Ok(mut value) = self.text.lock() { *value = None; }
        self.worker.abort();
    }
}
fn clipboard_serial_admit(seen: &mut std::collections::HashSet<u32>, serial: u32) -> bool {
    seen.len() < 64 && seen.insert(serial)
}
fn clipboard_transfer_allowed(same_session: bool, mime: &str, unique: bool, alive: bool, owns: bool) -> bool {
    same_session && unique && alive && owns && matches!(mime, "text/plain;charset=utf-8" | "text/plain")
}
fn clipboard_write(fd: std::os::fd::OwnedFd, text: &[u8], alive: &AtomicBool, owns: &AtomicBool, generation: &std::sync::atomic::AtomicU64, expected_generation: u64) -> bool {
    use std::os::fd::AsRawFd;
    let raw = fd.as_raw_fd();
    if unsafe { libc::fcntl(raw, libc::F_SETFL, libc::O_NONBLOCK) } < 0 { return false; }
    let until = std::time::Instant::now() + Duration::from_secs(2);
    let mut offset = 0;
    while offset < text.len() && alive.load(Ordering::Acquire) && owns.load(Ordering::Acquire) && generation.load(Ordering::Acquire) == expected_generation && std::time::Instant::now() < until {
        let mut poll = libc::pollfd { fd: raw, events: libc::POLLOUT, revents: 0 };
        if unsafe { libc::poll(&mut poll, 1, 100) } <= 0 { continue; }
        let written = unsafe { libc::write(raw, text[offset..].as_ptr().cast(), text.len() - offset) };
        if written <= 0 { return false; }
        offset += written as usize;
    }
    offset == text.len() && alive.load(Ordering::Acquire) && owns.load(Ordering::Acquire) && generation.load(Ordering::Acquire) == expected_generation
}

#[derive(Default)]
struct Real { handoff: Option<StreamHandoff>, remote: Option<std::os::fd::OwnedFd>, screencast: Option<Screencast>, serial: Option<u64>, frame_size: Option<(u32,u32)>, rd: Option<RemoteDesktop>, session: Option<&'static Session<RemoteDesktop>>, gone: std::sync::Arc<AtomicBool>, shortcuts: Option<Session<GlobalShortcuts>>, shortcut_description: Option<String>,
    clipboard: Option<ClipboardOwner>,
    #[cfg(feature = "cursor-overlay")] overlay: Option<overlay::Cursor> }

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamHandoff { path:String, secret:String, host_pid:u32, host_start:u64 }

// Called only after the control portal has selected its actual stream. The Host
// endpoint and process generation are authenticated before its FD is sent.
fn send_control_stream(h:StreamHandoff, fd:std::os::fd::OwnedFd, serial:u64, frame_size:Option<(u32,u32)>) -> Option<std::os::fd::OwnedFd> {
    use std::io::Read;
    use std::os::fd::AsRawFd;
    use std::os::unix::net::UnixStream;
    if h.path.len()>100 || h.secret.len()!=64 || !h.secret.bytes().all(|b|b.is_ascii_hexdigit()) || h.host_pid<2 || serial==0 {return None;}
    let mut stream=UnixStream::connect(&h.path).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
    let mut cred=std::mem::MaybeUninit::<libc::ucred>::uninit();
    let mut len=std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe{libc::getsockopt(stream.as_raw_fd(),libc::SOL_SOCKET,libc::SO_PEERCRED,cred.as_mut_ptr().cast(),&mut len)}!=0 {return None;}
    let cred=unsafe{cred.assume_init()};
    if cred.pid as u32!=h.host_pid || cred.uid!=unsafe{libc::geteuid()} {return None;}
    let stat=std::fs::read_to_string(format!("/proc/{}/stat",h.host_pid)).ok()?;
    let tail=stat.rsplit_once(')')?.1;
    if tail.split_whitespace().nth(19)?.parse::<u64>().ok()?!=h.host_start {return None;}
    let mut challenge=[0u8;64];stream.read_exact(&mut challenge).ok()?;
    if challenge.iter().zip(h.secret.as_bytes()).fold(0u8,|acc,(a,b)|acc|(a^b))!=0 {return None;}
    let mut payload=serial.to_be_bytes().to_vec();
    if let Some((width,height))=frame_size { if width==0 || height==0 {return None;} payload.extend(width.to_be_bytes());payload.extend(height.to_be_bytes()); }
    let mut iov=libc::iovec{iov_base:payload.as_ptr() as *mut _,iov_len:payload.len()};
    let space=unsafe{libc::CMSG_SPACE(std::mem::size_of::<libc::c_int>() as u32)} as usize;
    // usize backing ensures cmsghdr alignment.
    let mut control=vec![0usize;(space+std::mem::size_of::<usize>()-1)/std::mem::size_of::<usize>()];
    let mut msg:libc::msghdr=unsafe{std::mem::zeroed()};msg.msg_iov=&mut iov;msg.msg_iovlen=1;msg.msg_control=control.as_mut_ptr().cast();msg.msg_controllen=space;
    unsafe {
        let hdr=libc::CMSG_FIRSTHDR(&msg);if hdr.is_null(){return None;}
        (*hdr).cmsg_level=libc::SOL_SOCKET;(*hdr).cmsg_type=libc::SCM_RIGHTS;(*hdr).cmsg_len=libc::CMSG_LEN(std::mem::size_of::<libc::c_int>() as u32) as usize;
        std::ptr::write_unaligned(libc::CMSG_DATA(hdr).cast::<libc::c_int>(),fd.as_raw_fd());
        if libc::sendmsg(stream.as_raw_fd(),&msg,libc::MSG_NOSIGNAL)!=payload.len() as isize{return None;}
    }
    Some(fd)
}

/// Start on the same D-Bus connection used to create the session, retaining
/// the optional v6 stream serial that ashpd 0.13 does not expose.
async fn start_with_serial(connection:&zbus::Connection, session:&Session<RemoteDesktop>) -> Result<(ashpd::desktop::remote_desktop::SelectedDevices, Option<u64>),BeginErr> {
    use std::collections::HashMap;
    use futures_util::StreamExt;
    use zbus::{MatchRule, MessageStream, message::Type, zvariant::{OwnedObjectPath, OwnedValue, Value}};
    let mut random=[0u8;16];
    if unsafe{libc::getrandom(random.as_mut_ptr().cast(),random.len(),0)} != random.len() as isize {return Err(BeginErr::Unavailable);}
    let token=format!("oc_{}",random.iter().map(|v|format!("{v:02x}")).collect::<String>());
    let unique=connection.unique_name().ok_or(BeginErr::Unavailable)?.as_str().trim_start_matches(':').replace('.',"_");
    let path=format!("/org/freedesktop/portal/desktop/request/{unique}/{token}");
    let rule=MatchRule::builder().msg_type(Type::Signal).sender("org.freedesktop.portal.Desktop").map_err(|_|BeginErr::Unavailable)?.interface("org.freedesktop.portal.Request").map_err(|_|BeginErr::Unavailable)?
        .member("Response").map_err(|_|BeginErr::Unavailable)?.path(path.as_str()).map_err(|_|BeginErr::Unavailable)?.build();
    let mut signals=MessageStream::for_match_rule(rule,connection,Some(1)).await.map_err(|_|BeginErr::Unavailable)?;
    let proxy=zbus::Proxy::new(connection,"org.freedesktop.portal.Desktop","/org/freedesktop/portal/desktop","org.freedesktop.portal.RemoteDesktop").await.map_err(|_|BeginErr::Unavailable)?;
    let mut options=HashMap::new();options.insert("handle_token",Value::from(token.as_str()));
    let actual:OwnedObjectPath=proxy.call("Start",&(session,"",options)).await.map_err(|_|BeginErr::Unavailable)?;
    if actual.as_str()!=path {return Err(BeginErr::Unavailable);}
    let message=signals.next().await.ok_or(BeginErr::Unavailable)?.map_err(|_|BeginErr::Unavailable)?;
    let selected=match message.body().deserialize::<ashpd::desktop::Response<ashpd::desktop::remote_desktop::SelectedDevices>>().map_err(|_|BeginErr::Unavailable)? {
        ashpd::desktop::Response::Ok(value)=>value, ashpd::desktop::Response::Err(_)=>return Err(BeginErr::Denied)
    };
    let serial=(|| {
        let (_,mut values):(u32,HashMap<String,OwnedValue>)=message.body().deserialize().ok()?;
        let streams:Vec<(u32,HashMap<String,OwnedValue>)>=values.remove("streams")?.try_into().ok()?;
        if streams.len()!=1 || selected.streams().len()!=1 || streams[0].0!=selected.streams()[0].pipe_wire_node_id(){return None;}
        let (_,mut properties)=streams.into_iter().next()?;
        let value:u64=properties.remove("pipewire-serial")?.try_into().ok()?;
        (value>0).then_some(value)
    })();
    Ok((selected,serial))
}

struct ShortcutGuard(Option<Session<GlobalShortcuts>>);
impl Drop for ShortcutGuard {
    fn drop(&mut self) {
        if let Some(session) = self.0.take() {
            tokio::spawn(async move { let _ = session.close().await; });
        }
    }
}

async fn setup_takeover_shortcut(flag: std::sync::Arc<AtomicBool>) -> Result<(Session<GlobalShortcuts>, String), ()> {
    use futures_util::StreamExt;
    let portal = GlobalShortcuts::new().await.map_err(|_| ())?;
    let mut guard = ShortcutGuard(Some(portal.create_session(Default::default()).await.map_err(|_| ())?));
    let session = guard.0.as_ref().ok_or(())?;
    let session_path = serde_json::to_value(session).map_err(|_| ())?.as_str().ok_or(())?.to_owned();
    let id = "openclank-stop-control";
    let requested = [NewShortcut::new(id, "Stop Open Clank desktop control")];
    // Subscribe first so an activation immediately after binding is not missed.
    let mut activated = Box::pin(portal.receive_activated().await.map_err(|_| ())?);
    let binding = portal.bind_shortcuts(session, &requested, None, Default::default()).await.map_err(|_| ())?.response().map_err(|_| ())?;
    let description = binding.shortcuts().iter().find(|item| item.id() == id && !item.trigger_description().is_empty())
        .map(|item| item.trigger_description().chars().take(128).collect::<String>()).ok_or(())?;
    tokio::spawn(async move {
        while let Some(event) = activated.next().await {
            if event.shortcut_id() == id && event.session_handle().as_str() == session_path {
                flag.store(true, Ordering::Relaxed); break;
            }
        }
    });
    Ok((guard.0.take().ok_or(())?, description))
}
fn state(down: bool) -> KeyState { if down { KeyState::Pressed } else { KeyState::Released } }
impl Portal for Real {
    fn stream_handoff(&mut self, handoff: Option<StreamHandoff>) { self.handoff=handoff; }
    fn ready(&self) -> bool { std::env::var("XDG_SESSION_TYPE").ok().as_deref() == Some("wayland") && std::env::var_os("DBUS_SESSION_BUS_ADDRESS").is_some() }
    async fn begin(&mut self, p: &BeginParams) -> Result<StreamInfo, BeginErr> {
        let un = |_| BeginErr::Unavailable;
        let connection = Some(zbus::Connection::session().await.map_err(|_| BeginErr::Unavailable)?);
        let rd = if let Some(connection)=&connection { RemoteDesktop::with_connection(connection.clone()).await.map_err(un)? } else { RemoteDesktop::new().await.map_err(un)? };
        let sc = if let Some(connection)=&connection { Screencast::with_connection(connection.clone()).await.map_err(un)? } else { Screencast::new().await.map_err(un)? };
        let session = rd.create_session(Default::default()).await.map_err(un)?;
        // The portal closing the session (the human revoking it from the system indicator) is a takeover: watch it for the whole life.
        // One leaked handle per helper process (the helper serves exactly one control session), so the signal stream can be 'static.
        let session: &'static Session<RemoteDesktop> = Box::leak(Box::new(session));
        if let Ok(mut closed) = session.receive_closed().await.map(Box::pin) {
            let flag = self.gone.clone();
            tokio::spawn(async move { use futures_util::StreamExt; if closed.next().await.is_some() { flag.store(true, Ordering::Relaxed); } });
        }
        let session = self.session.insert(session);
        let session: &Session<RemoteDesktop> = session;
        let rd = self.rd.insert(rd);
        // Clipboard is requested only for an explicit replacement session and on this connection.
        let mut clipboard = if p.clipboard {
            ClipboardOwner::prepare(connection.as_ref().ok_or(BeginErr::Unavailable)?, session).await.ok()
        } else { None };
        // No restore token is ever set or read.
        rd.select_devices(session, SelectDevicesOptions::default().set_devices(DeviceType::Keyboard | DeviceType::Pointer).set_persist_mode(PersistMode::DoNot)).await.map_err(un)?.response().map_err(un)?;
        sc.select_sources(session, SelectSourcesOptions::default().set_cursor_mode(CursorMode::Embedded).set_sources(ashpd::enumflags2::BitFlags::from(SourceType::Monitor)).set_multiple(false)).await.map_err(un)?.response().map_err(un)?;
        let (started, serial) = if let Some(connection)=&connection { start_with_serial(connection,session).await? } else {
            (rd.start(session, None, Default::default()).await.map_err(un)?.response().map_err(|e| if matches!(e, ashpd::Error::Response(_)) { BeginErr::Denied } else { BeginErr::Unavailable })?,None)
        };
        if started.is_clipboard_enabled() { self.clipboard = clipboard.take(); }
        if let Some(mut owner) = clipboard { owner.close().await; }
        let streams = started.streams();
        if streams.len() != 1 || !started.devices().contains(DeviceType::Keyboard | DeviceType::Pointer) { return Err(BeginErr::Unsupported); }
        let s = &streams[0];
        match (s.source_type(), s.size()) {
            (Some(SourceType::Monitor), Some((w, h))) if w > 0 && h > 0 && w <= 65536 && h <= 65536 => {
                let info = StreamInfo { node: s.pipe_wire_node_id(), width: w as u32, height: h as u32 };
                if let (Some(handoff),Some(serial))=(self.handoff.take(),serial) {
                    if let Ok(fd)=sc.open_pipe_wire_remote(session,Default::default()).await {
                        self.remote=tokio::task::spawn_blocking(move||send_control_stream(handoff,fd,serial,None)).await.ok().flatten();
                    }
                }
                self.serial = serial;
                self.frame_size = Some((w as u32,h as u32));
                self.screencast = Some(sc);
                #[cfg(feature = "cursor-overlay")]
                if let Some((x, y)) = s.position() {
                    let rect = overlay::LogicalRect { x, y, width: w as u32, height: h as u32 };
                    self.overlay = tokio::task::spawn_blocking(move || overlay::start(rect)).await.ok().flatten();
                }
                if let Ok(Ok((shortcuts, description))) = tokio::time::timeout(Duration::from_secs(30), setup_takeover_shortcut(self.gone.clone())).await {
                    self.shortcuts = Some(shortcuts); self.shortcut_description = Some(description);
                }
                Ok(info)
            },
            _ => Err(BeginErr::Unsupported),
        }
    }
    async fn frame_handoff(&mut self, handoff: StreamHandoff) -> Result<(), ()> {
        if self.gone() {return Err(());}
        let (Some(sc),Some(session),Some(serial),Some(size))=(&self.screencast,&self.session,self.serial,self.frame_size) else{return Err(());};
        let fd=sc.open_pipe_wire_remote(*session,Default::default()).await.map_err(|_|())?;
        if self.gone(){return Err(());}
        let sent=tokio::task::spawn_blocking(move || send_control_stream(handoff,fd,serial,Some(size)).is_some()).await.map_err(|_|())?;
        if !sent || self.gone(){return Err(());} Ok(())
    }
    async fn motion_absolute(&mut self, node: u32, x: f64, y: f64) -> Result<(), ()> {
        let (Some(rd), Some(s)) = (&self.rd, &self.session) else { return Err(()) };
        rd.notify_pointer_motion_absolute(s, node, x, y, NotifyPointerMotionAbsoluteOptions::default()).await.map_err(|_| ())?;
        #[cfg(feature = "cursor-overlay")]
        if let Some(cursor) = &self.overlay { if cursor.is_active() { cursor.move_to(x as i32, y as i32); } }
        Ok(())
    }
    async fn button(&mut self, code: i32, down: bool) -> Result<(), ()> {
        let (Some(rd), Some(s)) = (&self.rd, &self.session) else { return Err(()) };
        rd.notify_pointer_button(s, code, state(down), NotifyPointerButtonOptions::default()).await.map_err(|_| ())
    }
    async fn keysym(&mut self, sym: i32, down: bool) -> Result<(), ()> {
        let (Some(rd), Some(s)) = (&self.rd, &self.session) else { return Err(()) };
        rd.notify_keyboard_keysym(s, sym, state(down), NotifyKeyboardKeysymOptions::default()).await.map_err(|_| ())
    }
    async fn axis(&mut self, vertical: bool, steps: i32) -> Result<(), ()> {
        let (Some(rd), Some(s)) = (&self.rd, &self.session) else { return Err(()) };
        rd.notify_pointer_axis_discrete(s, if vertical { Axis::Vertical } else { Axis::Horizontal }, steps, NotifyPointerAxisDiscreteOptions::default()).await.map_err(|_| ())
    }
    fn clipboard_enabled(&self) -> bool { self.clipboard.as_ref().is_some_and(|owner| owner.alive.load(Ordering::Acquire)) }
    fn clipboard_current(&self) -> bool { self.clipboard.as_ref().is_some_and(ClipboardOwner::current) }
    async fn clipboard_replace(&mut self, text: &str) -> Result<(), ()> {
        let owner = self.clipboard.as_mut().ok_or(())?;
        owner.replace(text).await
    }
    async fn probe(&mut self) -> Result<(u32, bool, bool), BeginErr> {
        let rd = RemoteDesktop::new().await.map_err(|_| BeginErr::Unavailable)?;
        let types = rd.available_device_types().await.map_err(|_| BeginErr::Unavailable)?;
        Ok((rd.version(), types.contains(DeviceType::Keyboard), types.contains(DeviceType::Pointer)))
    }
    fn gone(&self) -> bool {
        if self.gone.load(Ordering::Relaxed) { return true; }
        #[cfg(feature = "cursor-overlay")]
        if self.overlay.as_ref().is_some_and(|cursor| !cursor.is_active()) { return true; }
        false
    }
    fn recording_target(&self) -> Option<serde_json::Value> {
        #[cfg(feature = "cursor-overlay")]
        { self.overlay.as_ref()?.target().and_then(|target| serde_json::to_value(target).ok()) }
        #[cfg(not(feature = "cursor-overlay"))]
        { None }
    }
    fn shortcut_description(&self) -> Option<&str> { self.shortcut_description.as_deref() }
    fn overlay_at_begin(&self) -> Option<bool> {
        #[cfg(feature = "cursor-overlay")]
        { Some(self.overlay.as_ref().is_some_and(|cursor| cursor.is_active())) }
        #[cfg(not(feature = "cursor-overlay"))]
        { Some(false) }
    }
    async fn close(&mut self) {
        if let Some(mut owner) = self.clipboard.take() { owner.close().await; }
        self.handoff.take(); self.remote.take(); self.screencast.take(); self.serial.take(); self.frame_size.take();
        #[cfg(feature = "cursor-overlay")]
        { self.overlay.take(); }
        if let Some(s) = self.shortcuts.take() { let _ = s.close().await; }
        if let Some(s) = self.session.take() { let _ = s.close().await; }
    }
}

static TERM: AtomicBool = AtomicBool::new(false);
extern "C" fn on_term(_: libc::c_int) { TERM.store(true, Ordering::Relaxed); }

pub async fn run() {
    unsafe { libc::signal(libc::SIGTERM, on_term as extern "C" fn(libc::c_int) as libc::sighandler_t); }
    let (tx, mut rx) = mpsc::channel::<Option<Vec<u8>>>(1);
    tokio::spawn(async move {
        let mut input = BufReader::new(tokio::io::stdin());
        loop {
            let mut line = Vec::new();
            let n = (&mut input).take(MAX_LINE + 2).read_until(b'\n', &mut line).await;
            if !matches!(n, Ok(n) if n > 0) || line.len() as u64 > MAX_LINE || !line.ends_with(b"\n") { let _ = tx.send(None).await; break; }
            if tx.send(Some(line)).await.is_err() { break; }
        }
    });
    let mut out = |s: String| { let mut o = io::stdout().lock(); o.write_all(s.as_bytes()).and_then(|_| o.write_all(b"\n")).and_then(|_| o.flush()).is_ok() };
    let takeover = AtomicBool::new(false);
    session(&mut Real::default(), &mut rx, &mut out, &TERM, &takeover, LIMITS).await;
    if takeover.load(Ordering::Relaxed) { std::process::exit(73); }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Debug, PartialEq, Clone)]
    enum Call { Begin(BeginParams), Motion(u32, i64, i64), Button(i32, bool), Key(i32, bool), Axis(bool, i32), ClipSet(String), Probe, Close }
    struct Fake { calls: Vec<Call>, begin: Result<StreamInfo, BeginErr>, fail_at: Vec<usize>, input_calls: usize, ready: bool, clip: Option<String>, clip_set_fails: bool, probe: Result<(u32, bool, bool), BeginErr>, gone_at: Option<usize>, clipboard_requested: bool, clipboard_lost_at: Option<usize>, gone_signal: Option<std::sync::Arc<AtomicBool>> }
    fn fake() -> Fake { Fake { calls: vec![], begin: Ok(StreamInfo { node: 7, width: 1920, height: 1080 }), fail_at: vec![], input_calls: 0, ready: true, clip: Some("mine".into()), clip_set_fails: false, probe: Ok((2, true, true)), gone_at: None, clipboard_requested: false, clipboard_lost_at: None, gone_signal: None } }
    impl Fake {
        fn input(&mut self, c: Call) -> Result<(), ()> { self.calls.push(c); self.input_calls += 1; if self.fail_at.contains(&(self.input_calls - 1)) { Err(()) } else { Ok(()) } }
        fn closes(&self) -> usize { self.calls.iter().filter(|c| **c == Call::Close).count() }
        fn input_only(&self) -> Vec<Call> { self.calls.iter().filter(|c| !matches!(c, Call::Begin(_) | Call::Close | Call::ClipSet(_) | Call::Probe)).cloned().collect() }
    }
    impl Portal for Fake {
        fn ready(&self) -> bool { self.ready }
        async fn begin(&mut self, p: &BeginParams) -> Result<StreamInfo, BeginErr> { self.clipboard_requested = p.clipboard; self.calls.push(Call::Begin(p.clone())); self.begin }
        async fn motion_absolute(&mut self, n: u32, x: f64, y: f64) -> Result<(), ()> { self.input(Call::Motion(n, x as i64, y as i64)) }
        async fn button(&mut self, c: i32, d: bool) -> Result<(), ()> { self.input(Call::Button(c, d)) }
        async fn keysym(&mut self, s: i32, d: bool) -> Result<(), ()> { self.input(Call::Key(s, d)) }
        async fn axis(&mut self, v: bool, n: i32) -> Result<(), ()> { self.input(Call::Axis(v, n)) }
        fn clipboard_enabled(&self) -> bool { self.clipboard_requested && self.clip.is_some() }
        fn clipboard_current(&self) -> bool { self.clipboard_enabled() && !self.clipboard_lost_at.is_some_and(|n| self.input_calls >= n) }
        async fn clipboard_replace(&mut self, t: &str) -> Result<(), ()> { self.calls.push(Call::ClipSet(t.into())); if self.clip_set_fails { Err(()) } else { Ok(()) } }
        async fn probe(&mut self) -> Result<(u32, bool, bool), BeginErr> { self.calls.push(Call::Probe); self.probe }
        fn gone(&self) -> bool { self.gone_at.is_some_and(|n| self.input_calls >= n) || self.gone_signal.as_ref().is_some_and(|flag| flag.load(Ordering::Relaxed)) }
        async fn close(&mut self) { self.calls.push(Call::Close); }
    }
    const L: Limits = Limits { max_actions: 20, max_life: Duration::from_secs(120), min_gap: Duration::ZERO, max_deadline: Duration::from_secs(120) };
    const BEGIN_LINE: &str = r#"{"v":1,"op":"begin","deadline_ms":5000}"#;
    async fn drive(p: &mut Fake, lines: &[&str]) -> Vec<String> {
        let (tx, mut rx) = mpsc::channel(64);
        for l in lines { tx.send(Some(format!("{l}\n").into_bytes())).await.unwrap(); }
        tx.send(None).await.unwrap();
        let mut outs = vec![];
        session(p, &mut rx, &mut |s| { outs.push(s); true }, &AtomicBool::new(false), &AtomicBool::new(false), L).await;
        outs
    }
    fn click(x: i64, y: i64, w: u32, h: u32) -> String { format!(r#"{{"v":1,"op":"click","x":{x},"y":{y},"button":"left","frame":{{"width":{w},"height":{h}}}}}"#) }
    const ACTIVE: &str = r#"{"ok":true,"state":"active","stream":{"width":1920,"height":1080}}"#;

    #[tokio::test] async fn first_request_not_begin() {
        let mut p = fake(); let o = drive(&mut p, &[r#"{"v":1,"op":"stop"}"#, BEGIN_LINE]).await;
        assert_eq!(o, vec![r#"{"ok":false,"code":"not_begun"}"#]); assert!(p.calls.is_empty());
    }
    #[tokio::test] async fn begin_denied_and_no_session() {
        let mut p = fake(); p.begin = Err(BeginErr::Denied); let o = drive(&mut p, &[BEGIN_LINE]).await;
        assert_eq!(o, vec![r#"{"ok":false,"code":"denied"}"#]);
        let mut p = fake(); p.ready = false; let o = drive(&mut p, &[BEGIN_LINE]).await;
        assert_eq!(o, vec![r#"{"ok":false,"code":"no_wayland_session"}"#]); assert!(p.calls.is_empty());
    }
    #[tokio::test] async fn begin_params_never_carry_restore_token() {
        let mut p = fake(); drive(&mut p, &[BEGIN_LINE]).await;
        let Call::Begin(b) = &p.calls[0] else { panic!() };
        assert_eq!(b.restore_token, None); assert!(b.persist_do_not && b.keyboard && b.pointer && b.monitor_only && !b.multiple && b.cursor_embedded);
    }
    #[tokio::test] async fn click_sequence() {
        let mut p = fake(); let o = drive(&mut p, &[BEGIN_LINE, &click(10, 20, 1920, 1080)]).await;
        assert_eq!(o[0], ACTIVE); assert_eq!(o[1], r#"{"ok":true,"outcome":"sent","actions_left":19}"#);
        assert_eq!(p.input_only(), vec![Call::Motion(7, 10, 20), Call::Button(0x110, true), Call::Button(0x110, false)]);
        assert_eq!(p.closes(), 1);
    }
    #[tokio::test] async fn frame_mismatch_and_bounds_make_no_calls() {
        let mut p = fake();
        let o = drive(&mut p, &[BEGIN_LINE, &click(1, 1, 1280, 720), &click(1920, 0, 1920, 1080), &click(0, -1, 1920, 1080)]).await;
        assert_eq!(o[1], r#"{"ok":false,"code":"frame_mismatch"}"#);
        assert_eq!(o[2], r#"{"ok":false,"code":"out_of_bounds"}"#); assert_eq!(o[3], r#"{"ok":false,"code":"out_of_bounds"}"#);
        assert!(p.input_only().is_empty());
    }
    #[tokio::test] async fn key_chord_reverse_release() {
        let mut p = fake(); let o = drive(&mut p, &[BEGIN_LINE, r#"{"v":1,"op":"key","keysyms":[65507,99]}"#, r#"{"v":1,"op":"key","keysyms":[]}"#, r#"{"v":1,"op":"key","keysyms":[1,2,3,4,5]}"#]).await;
        assert_eq!(p.input_only(), vec![Call::Key(65507, true), Call::Key(99, true), Call::Key(99, false), Call::Key(65507, false)]);
        assert_eq!(o[2], r#"{"ok":false,"code":"invalid_request"}"#); assert_eq!(o[3], r#"{"ok":false,"code":"invalid_request"}"#);
    }
    #[tokio::test] async fn type_maps_keysyms_and_refuses_control() {
        let mut p = fake(); let o = drive(&mut p, &[BEGIN_LINE, r#"{"v":1,"op":"type","text":"aé€\n"}"#, r#"{"v":1,"op":"type","text":"x\u001b"}"#, r#"{"v":1,"op":"type","text":""}"#]).await;
        let syms: Vec<i32> = p.input_only().iter().filter_map(|c| if let Call::Key(s, true) = c { Some(*s) } else { None }).collect();
        assert_eq!(syms, vec![0x61, 0xe9, 0x0100_20ac, 0xff0d]);
        assert_eq!(p.input_only().len(), 8);
        assert_eq!(o[2], r#"{"ok":false,"code":"invalid_text"}"#); assert_eq!(o[3], r#"{"ok":false,"code":"invalid_text"}"#);
    }
    #[tokio::test] async fn action_cap_expires() {
        let mut p = fake(); let a = r#"{"v":1,"op":"key","keysyms":[97]}"#;
        let mut lines = vec![BEGIN_LINE]; lines.extend(std::iter::repeat(a).take(21)); lines.push(a);
        let o = drive(&mut p, &lines).await;
        assert_eq!(o[20], r#"{"ok":true,"outcome":"sent","actions_left":0}"#);
        assert_eq!(o[21], r#"{"ok":false,"code":"expired"}"#); assert_eq!(o.len(), 22);
        assert_eq!(p.closes(), 1); assert_eq!(p.input_only().len(), 40);
    }
    #[tokio::test] async fn failure_between_press_and_release() {
        let mut p = fake(); p.fail_at = vec![2]; // motion=0, press=1, release=2 fails
        let o = drive(&mut p, &[BEGIN_LINE, &click(1, 1, 1920, 1080), r#"{"v":1,"op":"key","keysyms":[97]}"#]).await;
        assert_eq!(o[1], r#"{"ok":false,"code":"outcome_unknown"}"#);
        // release attempted once more (not a retry of the click: no second motion/press), session kept
        assert_eq!(&p.input_only()[..5], &[Call::Motion(7, 1, 1), Call::Button(0x110, true), Call::Button(0x110, false), Call::Button(0x110, false), Call::Key(97, true)]);
        assert_eq!(o[2], r#"{"ok":true,"outcome":"sent","actions_left":18}"#); assert_eq!(p.closes(), 1);
    }
    #[tokio::test] async fn failed_release_closes_session() {
        let mut p = fake(); p.fail_at = vec![2, 3]; // release fails, and so does the release retry
        let o = drive(&mut p, &[BEGIN_LINE, &click(1, 1, 1920, 1080), r#"{"v":1,"op":"key","keysyms":[97]}"#]).await;
        assert_eq!(o, vec![ACTIVE, r#"{"ok":false,"code":"outcome_unknown"}"#]);
        assert_eq!(p.closes(), 1); assert_eq!(p.input_only().len(), 5);
        assert_eq!(p.input_only()[4], Call::Button(0x110, false));
    }
    #[tokio::test] async fn stop_and_eof_release_and_close_once() {
        let mut p = fake(); let o = drive(&mut p, &[BEGIN_LINE, r#"{"v":1,"op":"stop"}"#, r#"{"v":1,"op":"stop"}"#]).await;
        assert_eq!(o[1], r#"{"ok":true,"state":"stopped"}"#); assert_eq!(o.len(), 2); assert_eq!(p.closes(), 1);
        let mut p = fake(); let o = drive(&mut p, &[BEGIN_LINE]).await; assert_eq!(o.len(), 1); assert_eq!(p.closes(), 1);
    }
    #[tokio::test] async fn strict_parsing() {
        let mut p = fake();
        let o = drive(&mut p, &[BEGIN_LINE, r#"{"v":1,"op":"stop","extra":1}"#, r#"{"v":1,"op":"drag"}"#, r#"{"v":2,"op":"stop"}"#, r#"{"op":"stop"}"#]).await;
        for r in &o[1..] { assert_eq!(r, r#"{"ok":false,"code":"invalid_request"}"#); }
        assert_eq!(o.len(), 5); assert_eq!(p.closes(), 1);
    }

    fn frame() -> &'static str { r#""frame":{"width":1920,"height":1080}"# }
    #[tokio::test] async fn move_only_moves_the_pointer_and_validates_first() {
        let mut p = fake(); let m = |x: i64, y: i64, w: u32| format!(r#"{{"v":1,"op":"move","x":{x},"y":{y},"frame":{{"width":{w},"height":1080}}}}"#);
        let o = drive(&mut p, &[BEGIN_LINE, &m(5, 6, 1920), &m(1920, 0, 1920), &m(1, 1, 1280)]).await;
        assert_eq!(p.input_only(), vec![Call::Motion(7, 5, 6)], "no button, no key");
        assert_eq!(o[1], r#"{"ok":true,"outcome":"sent","actions_left":19}"#); assert_eq!(o[2], r#"{"ok":false,"code":"out_of_bounds"}"#); assert_eq!(o[3], r#"{"ok":false,"code":"frame_mismatch"}"#);
    }
    #[tokio::test] async fn probe_asks_the_portal_without_a_session() {
        let mut p = fake(); let o = drive(&mut p, &[r#"{"v":1,"op":"probe"}"#, BEGIN_LINE]).await;
        assert_eq!(o, vec![r#"{"ok":true,"state":"probe","version":2,"keyboard":true,"pointer":true}"#]); assert_eq!(p.calls, vec![Call::Probe]);
        let mut p = fake(); p.probe = Err(BeginErr::Unavailable); assert_eq!(drive(&mut p, &[r#"{"v":1,"op":"probe"}"#]).await, vec![r#"{"ok":false,"code":"unavailable"}"#]);
        let mut p = fake(); p.ready = false; assert_eq!(drive(&mut p, &[r#"{"v":1,"op":"probe"}"#]).await, vec![r#"{"ok":false,"code":"no_wayland_session"}"#]); assert!(p.calls.is_empty());
        let mut p = fake(); let o = drive(&mut p, &[BEGIN_LINE, r#"{"v":1,"op":"probe"}"#]).await; assert_eq!(o[1], r#"{"ok":false,"code":"invalid_request"}"#); assert!(!p.calls.contains(&Call::Probe));
    }
    #[tokio::test] async fn scroll_moves_then_uses_discrete_steps_and_validates_first() {
        let mut p = fake();
        let s = |d: &str, n: i64| format!(r#"{{"v":1,"op":"scroll","x":5,"y":6,"direction":"{d}","amount":{n},{}}}"#, frame());
        let o = drive(&mut p, &[BEGIN_LINE, &s("down", 3), &s("up", 2), &s("left", 1), &s("right", 4), &s("down", 0), &s("down", 21), &s("sideways", 1), r#"{"v":1,"op":"scroll","x":1920,"y":0,"direction":"up","amount":1,"frame":{"width":1920,"height":1080}}"#]).await;
        assert_eq!(p.input_only(), vec![Call::Motion(7, 5, 6), Call::Axis(true, 3), Call::Motion(7, 5, 6), Call::Axis(true, -2), Call::Motion(7, 5, 6), Call::Axis(false, -1), Call::Motion(7, 5, 6), Call::Axis(false, 4)]);
        assert_eq!(&o[5..7], [r#"{"ok":false,"code":"invalid_request"}"#, r#"{"ok":false,"code":"invalid_request"}"#]);
        assert_eq!(o[7], r#"{"ok":false,"code":"invalid_request"}"#); assert_eq!(o[8], r#"{"ok":false,"code":"out_of_bounds"}"#);
    }
    #[tokio::test] async fn drag_interpolates_and_releases_on_failure() {
        let mut p = fake(); let d = format!(r#"{{"v":1,"op":"drag","x1":10,"y1":10,"x2":130,"y2":70,{}}}"#, frame());
        let o = drive(&mut p, &[BEGIN_LINE, &d]).await; assert_eq!(o[1], r#"{"ok":true,"outcome":"sent","actions_left":19}"#);
        let calls = p.input_only(); assert_eq!(calls.len(), 1 + 1 + 12 + 1);
        assert_eq!(calls[0], Call::Motion(7, 10, 10)); assert_eq!(calls[1], Call::Button(0x110, true)); assert_eq!(calls[13], Call::Motion(7, 130, 70)); assert_eq!(calls[14], Call::Button(0x110, false));
        let mut p = fake(); p.fail_at = vec![5]; // fails mid-drag with the button held
        let o = drive(&mut p, &[BEGIN_LINE, &d, r#"{"v":1,"op":"key","keysyms":[97]}"#]).await;
        assert_eq!(o[1], r#"{"ok":false,"code":"outcome_unknown"}"#);
        let calls = p.input_only(); assert_eq!(calls.iter().filter(|c| **c == Call::Button(0x110, false)).count(), 1, "released once, not repeated"); assert_eq!(calls.iter().filter(|c| **c == Call::Button(0x110, true)).count(), 1);
        assert_eq!(o[2], r#"{"ok":true,"outcome":"sent","actions_left":18}"#);
        let mut p = fake(); let bad = format!(r#"{{"v":1,"op":"drag","x1":10,"y1":10,"x2":1920,"y2":70,{}}}"#, frame());
        let o = drive(&mut p, &[BEGIN_LINE, &bad]).await; assert_eq!(o[1], r#"{"ok":false,"code":"out_of_bounds"}"#); assert!(p.input_only().is_empty());
    }
    #[tokio::test] async fn explicit_clipboard_replacement_and_partial_input() {
        let mut legacy = fake();
        let result = drive(&mut legacy, &[BEGIN_LINE, r#"{"v":1,"op":"paste","text":"hello"}"#]).await;
        assert_eq!(result[1], r#"{"ok":false,"code":"unsupported_clipboard"}"#);
        assert!(!legacy.calls.iter().any(|c| matches!(c, Call::ClipSet(_))));
        let begin = r#"{"v":1,"op":"begin","deadline_ms":5000,"clipboard":"replace"}"#;
        let paste = r#"{"v":1,"op":"paste","text":"hello","clipboard":"replace"}"#;
        let mut ok = fake(); let result = drive(&mut ok, &[begin,paste]).await;
        assert_eq!(result[1], r#"{"ok":true,"clipboard":"replaced","input":"sent","lifetime":"session"}"#);
        assert!(ok.calls.contains(&Call::Begin(BeginParams { clipboard:true, ..BEGIN })));
        assert_eq!(ok.calls.iter().filter(|c| matches!(c,Call::ClipSet(_))).count(),1);
        let mut denied = fake(); denied.clip = None; let result = drive(&mut denied, &[begin,paste]).await;
        assert_eq!(result[1], r#"{"ok":false,"code":"unsupported_clipboard"}"#);
        let mut failed = fake(); failed.fail_at = vec![0]; let result = drive(&mut failed, &[begin,paste]).await;
        assert_eq!(result[1], r#"{"ok":false,"clipboard":"replaced","input":"unknown","lifetime":"session"}"#);
        assert_eq!(failed.calls.iter().filter(|c| matches!(c,Call::ClipSet(_))).count(),1);
        let mut lost_before = fake(); lost_before.clipboard_lost_at = Some(0);
        let result = drive(&mut lost_before, &[begin,paste]).await;
        assert_eq!(result[1], r#"{"ok":false,"clipboard":"replaced","input":"not_issued","lifetime":"session"}"#);
        assert!(lost_before.input_only().is_empty());
        let mut lost_during = fake(); lost_during.clipboard_lost_at = Some(1);
        let result = drive(&mut lost_during, &[begin,paste]).await;
        assert_eq!(result[1], r#"{"ok":false,"clipboard":"replaced","input":"unknown","lifetime":"session"}"#);
        assert_eq!(lost_during.input_only(), vec![Call::Key(0xffe3,true),Call::Key(0xffe3,false)]);
        assert_eq!(lost_during.calls.iter().filter(|c| matches!(c,Call::ClipSet(_))).count(),1);
        let mut serials = std::collections::HashSet::new();
        assert!(clipboard_serial_admit(&mut serials, 42)); assert!(!clipboard_serial_admit(&mut serials, 42)); assert!(clipboard_serial_admit(&mut serials, 0)); assert!(!clipboard_serial_admit(&mut serials, 0));
        assert!(clipboard_transfer_allowed(true,"text/plain;charset=utf-8",true,true,true));
        assert!(!clipboard_transfer_allowed(false,"text/plain;charset=utf-8",true,true,true));
        assert!(!clipboard_transfer_allowed(true,"text/plain;charset=utf-8",false,true,true));
        assert!(!clipboard_transfer_allowed(true,"text/plain;charset=utf-8",true,false,true));
        assert!(!clipboard_transfer_allowed(true,"text/plain;charset=utf-8",true,true,false));
        use std::os::fd::FromRawFd;
        let pair = || { let mut fds = [0;2]; assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) },0); unsafe { (std::os::fd::OwnedFd::from_raw_fd(fds[0]),std::os::fd::OwnedFd::from_raw_fd(fds[1])) } };
        let generation = std::sync::atomic::AtomicU64::new(3);
        let live = AtomicBool::new(true); let owns = AtomicBool::new(true);
        let (_read,write) = pair(); assert!(clipboard_write(write,b"sample",&live,&owns,&generation,3));
        let (_read,write) = pair(); assert!(!clipboard_write(write,b"sample",&live,&owns,&generation,2));
        owns.store(false, Ordering::Release);
        let (_read,write) = pair(); assert!(!clipboard_write(write,b"sample",&live,&owns,&generation,3));
        live.store(false, Ordering::Release);
        let (_read,write) = pair(); assert!(!clipboard_write(write,b"sample",&live,&owns,&generation,3));
    }
    #[tokio::test] async fn a_closed_portal_session_is_a_takeover_that_releases_and_stops() {
        let mut p = fake(); p.gone_at = Some(3); // after the first click (motion, press, release) the human revokes the session
        let o = drive(&mut p, &[BEGIN_LINE, &click(1, 1, 1920, 1080), r#"{"v":1,"op":"key","keysyms":[97]}"#]).await;
        assert_eq!(o, vec![ACTIVE, r#"{"ok":true,"outcome":"sent","actions_left":19}"#, r#"{"ok":false,"code":"taken_over"}"#]);
        assert_eq!(p.input_only().len(), 3, "nothing is sent after the takeover"); assert_eq!(p.closes(), 1);
    }
    #[tokio::test] async fn idle_portal_revocation_stops_without_another_request() {
        let mut p = fake();
        let revoked = std::sync::Arc::new(AtomicBool::new(false));
        p.gone_signal = Some(revoked.clone());
        let (tx, mut rx) = mpsc::channel(2);
        tx.send(Some(BEGIN_LINE.as_bytes().to_vec())).await.unwrap();
        let switch = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(20)).await;
            revoked.store(true, Ordering::Relaxed);
        });
        let mut output = Vec::new();
        let mut record = |line| { output.push(line); true };
        tokio::time::timeout(Duration::from_secs(1), session(&mut p, &mut rx, &mut record, &AtomicBool::new(false), &AtomicBool::new(false), L)).await.unwrap();
        switch.await.unwrap();
        assert_eq!(output, vec![ACTIVE, r#"{"ok":false,"code":"taken_over"}"#]);
        assert_eq!(p.closes(), 1);
        drop(tx);
    }

    #[tokio::test] async fn takeover_during_action_does_not_send_the_next_press() {
        let mut p = fake(); p.gone_at = Some(1); // first pointer move, then the human revokes
        let output = drive(&mut p, &[BEGIN_LINE, &click(1, 1, 1920, 1080)]).await;
        assert_eq!(output, vec![ACTIVE, r#"{"ok":false,"code":"taken_over"}"#]);
        assert_eq!(p.input_only(), vec![Call::Motion(7, 1, 1)]);
        assert_eq!(p.closes(), 1);
    }



}
