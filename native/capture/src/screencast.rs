//! Internal, single-use portal session. The restricted FD never crosses IPC.
use std::{io::{self, Write}, time::{Duration, SystemTime, UNIX_EPOCH}};
use ashpd::desktop::{PersistMode, screencast::{CursorMode, Screencast, SelectSourcesOptions, SourceType}};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tokio::{io::{AsyncBufReadExt, AsyncReadExt, BufReader}, sync::mpsc};
#[cfg(feature = "frames")]
use tokio::io::AsyncWriteExt;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Open { version: u8, action: String, generation: String, deadline_ms: u64 }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Stop { version: u8, action: String, generation: String }
#[derive(Serialize)]
struct Active<'a> { version: u8, generation: &'a str, state: &'static str, source_type: &'static str, logical_size: Option<(i32,i32)>, cursor: &'static str }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis().min(u64::MAX as u128) as u64 }
fn send<T: Serialize>(value: &T) { let mut out = io::stdout().lock(); let _ = serde_json::to_writer(&mut out, value); let _ = out.write_all(b"\n"); let _ = out.flush(); }

pub async fn run() { run_with_frames(false).await; }
pub async fn run_with_frames(with_frames: bool) {
    let (tx, mut rx) = mpsc::channel::<Option<Vec<u8>>>(1);
    tokio::spawn(async move {
        let mut input = BufReader::new(tokio::io::stdin());
        loop {
            let mut line = Vec::new();
            let result = (&mut input).take(8194).read_until(b'\n', &mut line).await;
            if !matches!(result, Ok(n) if n > 0) || line.len() > 8192 || !line.ends_with(b"\n") {
                let _ = tx.send(None).await; break;
            }
            if tx.send(Some(line)).await.is_err() { break; }
        }
    });
    let Some(Some(line)) = rx.recv().await else { return };
    let Ok(req) = serde_json::from_slice::<Open>(&line) else { return };
    if req.version != 2 || req.action != "open" || req.generation.len() != 32 || !req.generation.bytes().all(|x| x.is_ascii_hexdigit()) || req.deadline_ms <= now() || req.deadline_ms > now().saturating_add(120000) { return; }
    let remaining = Duration::from_millis(req.deadline_ms.saturating_sub(now()));
    let timer = tokio::time::sleep(remaining); tokio::pin!(timer);
    let portal = tokio::select! { result = Screencast::new() => match result { Ok(x) => x, Err(_) => return }, _ = rx.recv() => return, _ = &mut timer => return };
    let session = tokio::select! { result = portal.create_session(Default::default()) => match result { Ok(x) => x, Err(_) => return }, _ = rx.recv() => return, _ = &mut timer => return };
    let setup = async {
        let mut closed = session.receive_closed().await.map_err(|_| ())?;
        let options = SelectSourcesOptions::default().set_cursor_mode(CursorMode::Embedded)
            .set_sources(SourceType::Monitor | SourceType::Window).set_multiple(false).set_persist_mode(PersistMode::DoNot);
        tokio::select! {
            selected = portal.select_sources(&session, options) => { selected.map_err(|_| ())?.response().map_err(|_| ())?; },
            _ = closed.next() => return Err(()), _ = rx.recv() => return Err(()), _ = &mut timer => return Err(())
        }
        let response = tokio::select! {
            started = portal.start(&session, None, Default::default()) => started.map_err(|_| ())?.response().map_err(|_| ())?,
            _ = closed.next() => return Err(()), _ = rx.recv() => return Err(()), _ = &mut timer => return Err(())
        };
        let streams = response.streams(); if streams.len() != 1 { return Err(()); }
        let stream = &streams[0];
        let source_type = match stream.source_type() { Some(SourceType::Monitor) => "monitor", Some(SourceType::Window) => "window", _ => "unknown" };
        let size = stream.size().filter(|(w,h)| *w > 0 && *h > 0 && *w <= 65536 && *h <= 65536);
        let fd = tokio::select! {
            remote = portal.open_pipe_wire_remote(&session, Default::default()) => remote.map_err(|_| ())?,
            _ = closed.next() => return Err(()), _ = rx.recv() => return Err(()), _ = &mut timer => return Err(())
        };
        let _owned_remote = fd;
        let (loss_tx, mut loss_rx) = mpsc::channel::<()>(1);
        #[cfg(feature = "frames")]
        let mut frames = if with_frames { Some(crate::frame::Frames::new(&_owned_remote, stream.pipe_wire_node_id(), loss_tx.clone())?) } else { None };
        #[cfg(not(feature = "frames"))]
        if with_frames { return Err(()); }
        send(&Active { version:2, generation:&req.generation, state:"active", source_type, logical_size:size, cursor:"embedded" });
        loop {
            tokio::select! {
                incoming = rx.recv() => {
                    let Some(Some(line)) = incoming else { break };
                    let Ok(command) = serde_json::from_slice::<Stop>(&line) else { break };
                    if command.version != 2 || command.generation != req.generation { break; }
                    if command.action == "stop" { break; }
                    #[cfg(feature = "frames")]
                    if command.action == "frame" {
                        let Some(mut source) = frames.take() else { break };
                        let worker = tokio::task::spawn_blocking(move || { let result = source.latest(); (source, result) });
                        let completed = tokio::select! {
                            biased;
                            _ = closed.next() => break,
                            _ = &mut timer => break,
                            _ = loss_rx.recv() => break,
                            _ = rx.recv() => break,
                            result = worker => result,
                        };
                        let Ok((source, result)) = completed else { break };
                        frames = Some(source);
                        match result {
                            Ok(Some(frame)) => {
                                let header = serde_json::json!({"version":2,"generation":req.generation,"state":"frame", "geometry":frame.geometry,"pixel_size":[frame.width,frame.height],"pts_ns":frame.pts_ns,"png_bytes":frame.png.len()});
                                let mut bytes = match serde_json::to_vec(&header) { Ok(value) => value, Err(_) => break };
                                bytes.push(b'\n');
                                let mut out = tokio::io::stdout();
                                let write = async { out.write_all(&bytes).await?; out.write_all(&frame.png).await?; out.flush().await };
                                let result = tokio::select! {
                                    biased;
                                    _ = closed.next() => break,
                                    _ = &mut timer => break,
                                    _ = loss_rx.recv() => break,
                                    _ = rx.recv() => break,
                                    value = write => value,
                                };
                                if result.is_err() { break; }
                            },
                            Ok(None) => send(&serde_json::json!({"version":2,"generation":req.generation,"state":"no_frame"})),
                            Err(()) => break,
                        }
                        continue;
                    }
                    break;
                },
                _ = closed.next() => break, _ = &mut timer => break,
                _ = loss_rx.recv(), if with_frames => break
            }
        }
        Ok::<(), ()>(())
    };
    let _ = setup.await;
    let _ = tokio::time::timeout(Duration::from_secs(1), session.close()).await;
}
