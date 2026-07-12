use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{Cursor, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use chrono::Utc;
#[cfg(feature = "embedded-assets")]
use include_dir::{include_dir, Dir};
use serde_json::{json, Map, Value};
use zip::write::SimpleFileOptions;

mod db_api;

#[cfg(feature = "embedded-assets")]
static EMBEDDED_OUT: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/../out");

const NOTE_SUFFIXES: &[&str] = &["md", "markdown", "base", "canvas", "dclg"];
const TEXT_EXPORT_SUFFIXES: &[&str] = &["md", "markdown", "base", "canvas", "dclg"];
const ASSET_SUFFIXES: &[&str] = &["png", "jpg", "jpeg", "gif", "webp", "svg"];

#[derive(Debug, Clone)]
pub struct NativeApiConfig {
    #[allow(dead_code)]
    pub root_dir: PathBuf,
    #[allow(dead_code)]
    pub site_dir: PathBuf,
    pub data_file: PathBuf,
    pub seed_file: PathBuf,
    pub vault_dir: PathBuf,
}

impl NativeApiConfig {
    pub fn from_env() -> Self {
        let root_dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        Self::from_root(root_dir)
    }

    pub fn from_root(root_dir: PathBuf) -> Self {
        let root_dir = root_dir.canonicalize().unwrap_or(root_dir);
        let site_dir = root_dir.join("out");
        let data_file = root_dir.join("move-data.json");
        let seed_file = site_dir.join("data").join("move-data.json");
        let vault_dir = std::env::var_os("COPAL_VAULT_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| root_dir.join("sample-vault"));
        let vault_dir = vault_dir.canonicalize().unwrap_or(vault_dir);
        Self { root_dir, site_dir, data_file, seed_file, vault_dir }
    }

    fn ui_state_file(&self) -> PathBuf {
        self.vault_dir.join(".copal").join("ui-state.json")
    }
}

pub struct NativeApiServer {
    addr: SocketAddr,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl NativeApiServer {
    pub fn start(config: NativeApiConfig) -> std::io::Result<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        listener.set_nonblocking(true)?;
        let addr = listener.local_addr()?;
        let shutdown = Arc::new(AtomicBool::new(false));
        let db = open_db(&config);
        let state = Arc::new(ApiState { config, write_lock: Mutex::new(()), db, events: db_api::Broadcaster::new() });
        let thread_shutdown = shutdown.clone();
        let handle = thread::spawn(move || {
            while !thread_shutdown.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let state = state.clone();
                        thread::spawn(move || {
                            let _ = handle_stream(stream, state);
                        });
                    },
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    },
                    Err(_) => break,
                }
            }
        });
        Ok(Self { addr, shutdown, handle: Some(handle) })
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }
}

impl Drop for NativeApiServer {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::Relaxed);
        let _ = TcpStream::connect(self.addr);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

pub(crate) struct ApiState {
    pub(crate) config: NativeApiConfig,
    write_lock: Mutex<()>,
    /// Source of truth when present (COPAL_SOURCE=files disables it).
    pub(crate) db: Option<copal_db::Db>,
    /// The one data stream: every commit landing is broadcast here.
    pub(crate) events: db_api::Broadcaster,
}

/// Open the versioned store per the metaplan: data dir resolved via the
/// repo debug bit (`copal.toml`), auto-import of the vault + planning file
/// when the view is empty (one undoable operation).
fn open_db(config: &NativeApiConfig) -> Option<copal_db::Db> {
    if std::env::var("COPAL_SOURCE").ok().as_deref() == Some("files") {
        println!("copal_db=disabled (COPAL_SOURCE=files)");
        return None;
    }
    let data_dir = copal_db::resolve_data_dir(&config.root_dir);
    match copal_db::Db::open(&data_dir) {
        Ok(db) => {
            println!("copal_db={}", data_dir.display());
            if db.is_empty().unwrap_or(false) {
                let planning = config
                    .data_file
                    .exists()
                    .then(|| config.data_file.clone())
                    .or_else(|| config.seed_file.exists().then(|| config.seed_file.clone()));
                match db.import_vault(&config.vault_dir, planning.as_deref()) {
                    Ok(stats) => println!(
                        "copal_db_import=notes:{} assets:{} planning:{}",
                        stats.notes, stats.assets, stats.planning
                    ),
                    Err(error) => eprintln!("copal_db_import_error={error}"),
                }
            }
            Some(db)
        },
        Err(error) => {
            eprintln!("copal_db_error={error} (falling back to files)");
            None
        },
    }
}

#[derive(Debug)]
struct Request {
    method: String,
    target: String,
    body: Vec<u8>,
}

#[derive(Debug)]
pub(crate) struct Response {
    code: u16,
    ctype: String,
    body: Vec<u8>,
    extra_headers: Vec<(String, String)>,
}

impl Response {
    pub(crate) fn new(code: u16, body: impl Into<Vec<u8>>, ctype: &str) -> Self {
        Self { code, ctype: ctype.to_string(), body: body.into(), extra_headers: Vec::new() }
    }

    pub(crate) fn json(code: u16, value: Value) -> Self {
        Self::new(code, json_bytes(&value), "application/json; charset=utf-8")
    }

    pub(crate) fn error(code: u16, message: &str) -> Self {
        Self::json(code, json!({ "error": message }))
    }
}

fn handle_stream(mut stream: TcpStream, state: Arc<ApiState>) -> std::io::Result<()> {
    let request = read_request(&mut stream)?;
    // The one data stream: long-lived SSE subscription, everything else is
    // one-shot request/response.
    if request.method == "GET" && request.target.split('?').next() == Some("/api/events") {
        return state.events.subscribe(stream);
    }
    let response = handle_request(&state, request);
    write_response(&mut stream, response)
}

fn read_request(stream: &mut TcpStream) -> std::io::Result<Request> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut buf = Vec::new();
    let mut tmp = [0_u8; 1024];
    let header_end = loop {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break None;
        }
        buf.extend_from_slice(&tmp[..n]);
        if let Some(pos) = find_header_end(&buf) {
            break Some(pos);
        }
        if buf.len() > 1024 * 1024 {
            break None;
        }
    }
    .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidData, "missing headers"))?;

    let header_bytes = &buf[..header_end];
    let header = String::from_utf8_lossy(header_bytes);
    let mut lines = header.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("/").to_string();
    let mut content_length = 0_usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }

    let body_start = header_end + 4;
    let mut body = buf.get(body_start..).unwrap_or_default().to_vec();
    while body.len() < content_length {
        let n = stream.read(&mut tmp)?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
    }
    body.truncate(content_length);
    Ok(Request { method, target, body })
}

fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|window| window == b"\r\n\r\n")
}

fn write_response(stream: &mut TcpStream, response: Response) -> std::io::Result<()> {
    let status = match response.code {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        _ => "OK",
    };
    write!(
        stream,
        "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n",
        response.code,
        status,
        response.ctype,
        response.body.len()
    )?;
    for (name, value) in response.extra_headers {
        write!(stream, "{}: {}\r\n", name, value)?;
    }
    stream.write_all(b"\r\n")?;
    stream.write_all(&response.body)
}

fn handle_request(state: &ApiState, request: Request) -> Response {
    let (path, query) = split_target(&request.target);
    // DB-native routes (/api/docs, /api/doc*, /api/ops, /api/undo,
    // /api/import/vault, /api/asset) are owned by db_api.
    if let Some(response) = db_api::handle(state, request.method.as_str(), &path, &query, &request.body) {
        return response;
    }
    let db = state.db.as_ref();
    match (request.method.as_str(), path.as_str()) {
        ("GET" | "HEAD", "/api/data") => match db.and_then(db_api::planning_get) {
            Some(bytes) => Response::new(200, bytes, "application/json; charset=utf-8"),
            None => Response::new(200, read_data(&state.config), "application/json; charset=utf-8"),
        },
        ("POST", "/api/data") => match db {
            Some(db) => db_api::planning_post(state, db, &request.body),
            None => handle_data_post(state, &request.body),
        },
        ("GET" | "HEAD", "/api/vault") => Response::json(200, json!({ "path": vault_label(state), "exists": true })),
        ("GET" | "HEAD", "/api/ui-state") => Response::json(200, read_ui_state(&state.config)),
        ("POST", "/api/ui-state") => handle_ui_state_post(state, &request.body),
        ("GET" | "HEAD", "/api/notes") => match db {
            Some(db) => db_api::legacy_notes(db, &vault_label(state)),
            None => Response::json(200, json!({ "vaultPath": state.config.vault_dir.to_string_lossy(), "notes": list_notes(&state.config) })),
        },
        ("GET" | "HEAD", "/api/note") => match db {
            Some(db) => db_api::legacy_note_get(db, &query),
            None => handle_note_get(state, &query),
        },
        ("POST", "/api/note") => match db {
            Some(db) => db_api::legacy_note_post(state, db, &request.body),
            None => handle_note_post(state, &request.body),
        },
        ("POST", "/api/note/rename") => match db {
            Some(db) => db_api::legacy_note_rename(state, db, &request.body),
            None => handle_note_rename(state, &request.body),
        },
        ("POST", "/api/note/delete") => match db {
            Some(db) => db_api::legacy_note_delete(state, db, &request.body),
            None => handle_note_delete(state, &request.body),
        },
        ("POST", "/api/mkdir") => match db {
            // Directories are virtual in the DB (names carry the path).
            Some(_) => Response::json(200, json!({ "ok": true })),
            None => handle_mkdir(state, &request.body),
        },
        ("GET" | "HEAD", "/api/index") => Response::json(200, build_vault_index(state)),
        ("GET" | "HEAD", "/api/search") => handle_search(state, &query),
        ("GET" | "HEAD", "/api/backlinks") => handle_backlinks(state, &query),
        ("GET" | "HEAD", "/api/graph") => Response::json(200, build_vault_index(state)["graph"].clone()),
        ("GET" | "HEAD", "/api/tasks") => Response::json(200, json!({ "tasks": build_vault_index(state)["tasks"].clone() })),
        ("GET" | "HEAD", "/api/vault-asset") => match db.and_then(|db| db_api::legacy_vault_asset(db, &query)) {
            Some(response) => response,
            None => handle_vault_asset(state, &query),
        },
        ("GET" | "HEAD", "/api/export/ai") => Response::json(200, export_ai(state)),
        ("GET" | "HEAD", "/api/export/okf") => Response::json(200, export_okf(state)),
        ("GET" | "HEAD", "/api/export/doclang") => Response::new(200, export_doclang(state), "application/xml; charset=utf-8"),
        ("GET" | "HEAD", "/api/export/markdown-bundle") => {
            let mut response = Response::new(200, export_markdown_bundle(state), "application/zip");
            response.extra_headers.push(("Content-Disposition".to_string(), "attachment; filename=\"copal-vault-markdown-bundle.zip\"".to_string()));
            response
        },
        // Vanilla UI (metaplan: copal-vanilla-ui). Served from <root>/ui/ on
        // disk while the classic app stays at /; embedded at V5 cutover.
        ("GET" | "HEAD", _) if path == "/ui" || path.starts_with("/ui/") => serve_ui(&state.config, &path),
        ("GET" | "HEAD", _) => serve_static(&state.config, &path),
        _ => Response::new(404, "not found", "text/plain; charset=utf-8"),
    }
}

fn serve_ui(config: &NativeApiConfig, path: &str) -> Response {
    let rel = path.trim_start_matches("/ui").trim_start_matches('/');
    let rel = if rel.is_empty() { "index.html" } else { rel };
    let ui_dir = config.root_dir.join("ui");
    let mut candidate = ui_dir.clone();
    for part in rel.split('/') {
        if part.is_empty() {
            continue;
        }
        if Path::new(part).components().any(|component| !matches!(component, Component::Normal(_))) {
            return Response::new(404, "not found", "text/plain; charset=utf-8");
        }
        candidate.push(part);
    }
    if !path_is_under(&candidate, &ui_dir) || !candidate.is_file() {
        return Response::new(404, "ui file not found (run from the repo root; ui/ is served from disk)", "text/plain; charset=utf-8");
    }
    Response::new(200, fs::read(&candidate).unwrap_or_default(), mime_for(&candidate))
}

/// Human-facing "vault" label: the DB data dir when the DB is active, the
/// on-disk vault dir in files mode.
fn vault_label(state: &ApiState) -> String {
    match state.db.as_ref() {
        Some(db) => db.assets_dir().parent().unwrap_or(db.assets_dir()).to_string_lossy().into_owned(),
        None => state.config.vault_dir.to_string_lossy().into_owned(),
    }
}

/// (entry-metadata, content) pairs from whichever source is active — the
/// single input for the index, search, and every exporter.
fn state_note_pairs(state: &ApiState) -> Vec<(Value, String)> {
    match state.db.as_ref() {
        Some(db) => db_api::note_pairs(db),
        None => list_notes(&state.config)
            .into_iter()
            .filter_map(|entry| {
                let note = read_note_entry(&state.config, &entry)?;
                let content = note["content"].as_str()?.to_string();
                Some((entry, content))
            })
            .collect(),
    }
}

fn split_target(target: &str) -> (String, BTreeMap<String, String>) {
    let (path, raw_query) = target.split_once('?').unwrap_or((target, ""));
    let mut query = BTreeMap::new();
    for pair in raw_query.split('&').filter(|item| !item.is_empty()) {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        query.insert(percent_decode(key), percent_decode(value));
    }
    (percent_decode(path), query)
}

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            },
            b'%' if i + 2 < bytes.len() => {
                if let Ok(hex) = std::str::from_utf8(&bytes[i + 1..i + 3]) {
                    if let Ok(value) = u8::from_str_radix(hex, 16) {
                        out.push(value);
                        i += 3;
                        continue;
                    }
                }
                out.push(bytes[i]);
                i += 1;
            },
            byte => {
                out.push(byte);
                i += 1;
            },
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn json_bytes(value: &Value) -> Vec<u8> {
    serde_json::to_string_pretty(value).unwrap_or_else(|_| "{}".to_string()).into_bytes()
}

fn read_data(config: &NativeApiConfig) -> Vec<u8> {
    fs::read(&config.data_file)
        .or_else(|_| fs::read(&config.seed_file))
        .unwrap_or_else(|_| b"{}".to_vec())
}

fn handle_data_post(state: &ApiState, body: &[u8]) -> Response {
    if serde_json::from_slice::<Value>(body).is_err() {
        return Response::new(400, "{\"error\":\"invalid json\"}", "application/json");
    }
    let _guard = state.write_lock.lock().unwrap();
    let tmp = state.config.data_file.with_extension("json.tmp");
    if let Err(error) = fs::write(&tmp, body).and_then(|_| fs::rename(&tmp, &state.config.data_file)) {
        return Response::error(400, &error.to_string());
    }
    Response::new(200, "{\"ok\":true}", "application/json")
}

fn read_json_object(body: &[u8]) -> Result<Map<String, Value>, String> {
    let value: Value = serde_json::from_slice(body).map_err(|error| error.to_string())?;
    value.as_object().cloned().ok_or_else(|| "expected json object".to_string())
}

fn read_ui_state(config: &NativeApiConfig) -> Value {
    let path = config.ui_state_file();
    let Ok(text) = fs::read_to_string(path) else {
        return json!({ "pinnedInfantecimemes": [] });
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(value) if value.is_object() => value,
        _ => json!({ "pinnedInfantecimemes": [] }),
    }
}

fn handle_ui_state_post(state: &ApiState, body: &[u8]) -> Response {
    let incoming = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let _guard = state.write_lock.lock().unwrap();
    let mut current = read_ui_state(&state.config).as_object().cloned().unwrap_or_default();
    current.extend(incoming);
    let path = state.config.ui_state_file();
    if let Err(error) = write_json_atomic(&path, &Value::Object(current.clone())) {
        return Response::error(400, &error.to_string());
    }
    Response::json(200, json!({ "ok": true, "state": Value::Object(current) }))
}

fn write_json_atomic(path: &Path, value: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, json_bytes(value))?;
    fs::rename(tmp, path)
}

fn vault_path(config: &NativeApiConfig, rel_path: &str) -> Option<PathBuf> {
    let clean = rel_path.replace('\\', "/").trim_start_matches('/').to_string();
    let mut out = config.vault_dir.clone();
    for part in clean.split('/') {
        if part.is_empty() {
            continue;
        }
        let part_path = Path::new(part);
        if part_path.components().any(|component| !matches!(component, Component::Normal(_))) {
            return None;
        }
        out.push(part);
    }
    if path_is_under(&out, &config.vault_dir) { Some(out) } else { None }
}

fn path_is_under(path: &Path, root: &Path) -> bool {
    let path_abs = absolutize(path);
    let root_abs = absolutize(root);
    path_abs.starts_with(root_abs)
}

fn absolutize(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")).join(path)
    }
}

fn suffix(path: &Path) -> String {
    path.extension().and_then(|value| value.to_str()).unwrap_or("").to_ascii_lowercase()
}

fn is_note_path(path: &Path) -> bool {
    NOTE_SUFFIXES.contains(&suffix(path).as_str())
}

fn is_text_export_path(path: &Path) -> bool {
    TEXT_EXPORT_SUFFIXES.contains(&suffix(path).as_str())
}

fn is_asset_path(path: &Path) -> bool {
    ASSET_SUFFIXES.contains(&suffix(path).as_str())
}

fn is_exportable_vault_file(config: &NativeApiConfig, path: &Path) -> bool {
    let Ok(rel) = path.strip_prefix(&config.vault_dir) else { return false };
    if rel.components().any(|component| {
        let Component::Normal(value) = component else { return true };
        let value = value.to_string_lossy();
        value.starts_with('.') || matches!(value.as_ref(), "node_modules" | "__pycache__" | "target")
    }) {
        return false;
    }
    path.is_file() && is_note_path(path)
}

fn list_notes(config: &NativeApiConfig) -> Vec<Value> {
    let mut files = Vec::new();
    collect_files(&config.vault_dir, &mut files);
    files
        .into_iter()
        .filter(|path| is_exportable_vault_file(config, path))
        .filter_map(|path| note_meta(config, &path))
        .collect()
}

fn collect_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_files(&path, out);
        } else {
            out.push(path);
        }
    }
    out.sort();
}

fn note_meta(config: &NativeApiConfig, path: &Path) -> Option<Value> {
    let rel = path.strip_prefix(&config.vault_dir).ok()?.to_string_lossy().replace('\\', "/");
    let stat = path.metadata().ok()?;
    Some(json!({
        "path": rel,
        "name": path.file_name()?.to_string_lossy(),
        "suffix": format!(".{}", suffix(path)),
        "size": stat.len(),
        "mtime": mtime_secs(&stat),
    }))
}

fn mtime_secs(stat: &fs::Metadata) -> f64 {
    stat.modified()
        .ok()
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn read_note_entry(config: &NativeApiConfig, entry: &Value) -> Option<Value> {
    let path = entry["path"].as_str()?;
    let fp = vault_path(config, path)?;
    let content = fs::read_to_string(fp).ok()?;
    let mut out = entry.as_object()?.clone();
    out.insert("content".to_string(), Value::String(content));
    Some(Value::Object(out))
}

fn note_title(path: &str, content: &str) -> String {
    for line in content.lines() {
        if let Some(value) = line.trim().strip_prefix("title:") {
            return value.trim().trim_matches(['"', '\'']).to_string();
        }
    }
    for line in content.lines() {
        if let Some(value) = line.strip_prefix("# ") {
            return value.trim().to_string();
        }
    }
    Path::new(path).file_stem().and_then(|value| value.to_str()).unwrap_or(path).to_string()
}

fn handle_note_get(state: &ApiState, query: &BTreeMap<String, String>) -> Response {
    let Some(note_path) = query.get("path") else { return Response::new(404, "{\"error\":\"note not found\"}", "application/json") };
    let Some(fp) = vault_path(&state.config, note_path) else { return Response::new(404, "{\"error\":\"note not found\"}", "application/json") };
    if !fp.is_file() || !is_note_path(&fp) {
        return Response::new(404, "{\"error\":\"note not found\"}", "application/json");
    }
    let rel = fp.strip_prefix(&state.config.vault_dir).unwrap_or(&fp).to_string_lossy().replace('\\', "/");
    let content = fs::read_to_string(&fp).unwrap_or_default();
    let stat = fp.metadata().ok();
    Response::json(200, json!({
        "path": rel,
        "name": fp.file_name().and_then(|value| value.to_str()).unwrap_or(""),
        "suffix": format!(".{}", suffix(&fp)),
        "content": content,
        "mtime": stat.as_ref().map(mtime_secs).unwrap_or(0.0),
    }))
}

fn handle_note_post(state: &ApiState, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let rel_path = data.get("path").and_then(Value::as_str).unwrap_or("");
    let content = data.get("content").and_then(Value::as_str).unwrap_or("");
    let Some(fp) = vault_path(&state.config, rel_path) else { return Response::error(400, "invalid note path") };
    if !is_note_path(&fp) {
        return Response::error(400, "invalid note path");
    }
    let _guard = state.write_lock.lock().unwrap();
    let backup = backup_file(&state.config, &fp);
    if let Some(parent) = fp.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            return Response::error(400, &error.to_string());
        }
    }
    let tmp = fp.with_extension(format!("{}.tmp", suffix(&fp)));
    if let Err(error) = fs::write(&tmp, content).and_then(|_| fs::rename(&tmp, &fp)) {
        return Response::error(400, &error.to_string());
    }
    let backup_rel = backup.as_ref().and_then(|path| path.strip_prefix(&state.config.vault_dir).ok()).map(|path| path.to_string_lossy().replace('\\', "/"));
    Response::json(200, json!({ "ok": true, "path": fp.strip_prefix(&state.config.vault_dir).unwrap_or(&fp).to_string_lossy().replace('\\', "/"), "backup": backup_rel }))
}

fn handle_note_rename(state: &ApiState, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let Some(src) = vault_path(&state.config, data.get("path").and_then(Value::as_str).unwrap_or("")) else {
        return Response::error(400, "invalid note path");
    };
    let Some(dst) = vault_path(&state.config, data.get("newPath").and_then(Value::as_str).unwrap_or("")) else {
        return Response::error(400, "invalid note path");
    };
    if !is_note_path(&src) || !is_note_path(&dst) {
        return Response::error(400, "invalid note path");
    }
    if !src.is_file() {
        return Response::error(404, "source note not found");
    }
    if dst.exists() {
        return Response::error(409, "destination exists");
    }
    let _guard = state.write_lock.lock().unwrap();
    let backup = backup_file(&state.config, &src);
    if let Some(parent) = dst.parent() {
        if let Err(error) = fs::create_dir_all(parent) {
            return Response::error(400, &error.to_string());
        }
    }
    if let Err(error) = fs::rename(&src, &dst) {
        return Response::error(400, &error.to_string());
    }
    let backup_rel = backup.as_ref().and_then(|path| path.strip_prefix(&state.config.vault_dir).ok()).map(|path| path.to_string_lossy().replace('\\', "/"));
    Response::json(200, json!({ "ok": true, "path": dst.strip_prefix(&state.config.vault_dir).unwrap_or(&dst).to_string_lossy().replace('\\', "/"), "backup": backup_rel }))
}

fn handle_note_delete(state: &ApiState, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let Some(fp) = vault_path(&state.config, data.get("path").and_then(Value::as_str).unwrap_or("")) else {
        return Response::error(400, "invalid note path");
    };
    if !is_note_path(&fp) {
        return Response::error(400, "invalid note path");
    }
    if !fp.is_file() {
        return Response::error(404, "note not found");
    }
    let _guard = state.write_lock.lock().unwrap();
    let backup = backup_file(&state.config, &fp);
    if let Err(error) = fs::remove_file(&fp) {
        return Response::error(400, &error.to_string());
    }
    let backup_rel = backup.as_ref().and_then(|path| path.strip_prefix(&state.config.vault_dir).ok()).map(|path| path.to_string_lossy().replace('\\', "/"));
    Response::json(200, json!({ "ok": true, "backup": backup_rel }))
}

fn handle_mkdir(state: &ApiState, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let Some(fp) = vault_path(&state.config, data.get("path").and_then(Value::as_str).unwrap_or("")) else {
        return Response::error(400, "invalid directory path");
    };
    if let Err(error) = fs::create_dir_all(&fp) {
        return Response::error(400, &error.to_string());
    }
    Response::json(200, json!({ "ok": true, "path": fp.strip_prefix(&state.config.vault_dir).unwrap_or(&fp).to_string_lossy().replace('\\', "/") }))
}

fn backup_file(config: &NativeApiConfig, fp: &Path) -> Option<PathBuf> {
    if !fp.is_file() {
        return None;
    }
    let rel = fp.strip_prefix(&config.vault_dir).ok()?;
    let dest = config.vault_dir.join(".copal").join("backups").join(now_stamp()).join(rel);
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).ok()?;
    }
    fs::copy(fp, &dest).ok()?;
    Some(dest)
}

fn now_stamp() -> String {
    Utc::now().format("%Y%m%d-%H%M%S-%6f").to_string()
}

fn build_vault_index(state: &ApiState) -> Value {
    let mut notes = Vec::new();
    let mut by_title = BTreeMap::new();
    for (entry, content) in state_note_pairs(state) {
        let Some(mut note) = entry.as_object().cloned().map(Value::Object) else { continue };
        let path = note["path"].as_str().unwrap_or("").to_string();
        let title = note_title(&path, &content);
        note.as_object_mut().unwrap().insert("content".to_string(), Value::String(content.clone()));
        note.as_object_mut().unwrap().insert("title".to_string(), Value::String(title.clone()));
        note.as_object_mut().unwrap().insert("wikilinks".to_string(), json!(wikilinks(&content)));
        note.as_object_mut().unwrap().insert("tags".to_string(), json!(tags(&content)));
        by_title.insert(title.to_ascii_lowercase(), path.clone());
        by_title.insert(Path::new(&path).file_stem().and_then(|value| value.to_str()).unwrap_or(&path).to_ascii_lowercase(), path);
        notes.push(note);
    }

    let mut tasks = Vec::new();
    let mut nodes: BTreeMap<String, Value> = BTreeMap::new();
    let mut edges = Vec::new();
    let mut backlinks: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for note in &notes {
        let path = note["path"].as_str().unwrap_or("");
        let title = note["title"].as_str().unwrap_or("");
        let content = note["content"].as_str().unwrap_or("");
        nodes.insert(path.to_string(), json!({ "id": path, "label": title, "path": path, "type": "note" }));
        let link_lines = wikilink_lines(content);
        for link in wikilinks(content) {
            let target = by_title.get(&link.to_ascii_lowercase()).cloned();
            let target_id = target.clone().unwrap_or_else(|| format!("missing:{}", link.to_ascii_lowercase()));
            nodes.entry(target_id.clone()).or_insert_with(|| json!({ "id": target_id, "label": link, "path": target, "type": if target.is_some() { "note" } else { "missing" } }));
            edges.push(json!({ "id": format!("{path}->{target_id}:wikilink"), "from": path, "to": target_id, "type": "wikilink" }));
            if let Some(target) = target {
                backlinks.entry(target).or_default().push(json!({ "sourcePath": path, "sourceTitle": title, "type": "wikilink", "line": link_lines.get(&link).copied().unwrap_or(1) }));
            }
        }
        for tag in tags(content) {
            let tag_id = format!("tag:{tag}");
            nodes.entry(tag_id.clone()).or_insert_with(|| json!({ "id": tag_id, "label": format!("#{tag}"), "type": "tag" }));
            edges.push(json!({ "id": format!("{path}->{tag_id}:tag"), "from": path, "to": tag_id, "type": "tag" }));
        }
        for (line_no, line) in content.lines().enumerate() {
            if let Some(task) = parse_task(path, title, line_no + 1, line) {
                tasks.push(task);
            }
        }
    }
    let notes_without_content: Vec<Value> = notes
        .iter()
        .filter_map(|note| {
            let mut object = note.as_object()?.clone();
            object.remove("content");
            Some(Value::Object(object))
        })
        .collect();
    json!({
        "vaultPath": vault_label(state),
        "notes": notes_without_content,
        "tasks": tasks,
        "graph": { "nodes": nodes.into_values().collect::<Vec<_>>(), "edges": edges },
        "backlinks": backlinks,
    })
}

fn wikilinks(content: &str) -> Vec<String> {
    let mut out = BTreeSet::new();
    let mut rest = content;
    while let Some(start) = rest.find("[[") {
        rest = &rest[start + 2..];
        let Some(end) = rest.find("]]") else { break };
        let raw = &rest[..end];
        let target = raw.split(['|', '#']).next().unwrap_or("").trim();
        if !target.is_empty() {
            out.insert(target.to_string());
        }
        rest = &rest[end + 2..];
    }
    out.into_iter().collect()
}

fn wikilink_lines(content: &str) -> BTreeMap<String, usize> {
    let mut out = BTreeMap::new();
    for (idx, line) in content.lines().enumerate() {
        for link in wikilinks(line) {
            out.entry(link).or_insert(idx + 1);
        }
    }
    out
}

fn tags(content: &str) -> Vec<String> {
    let mut out = BTreeSet::new();
    for token in content.split_whitespace() {
        let token = token.trim_matches(|c: char| matches!(c, '(' | ')' | '[' | ']' | ',' | '.' | ';' | ':'));
        if let Some(tag) = token.strip_prefix('#') {
            if !tag.is_empty() && tag.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '/' | '-')) {
                out.insert(tag.to_string());
            }
        }
    }
    out.into_iter().collect()
}

fn parse_task(path: &str, title: &str, line_no: usize, line: &str) -> Option<Value> {
    let trimmed = line.trim_start();
    let rest = trimmed.strip_prefix("- [").or_else(|| trimmed.strip_prefix("* ["))?;
    let mut chars = rest.chars();
    let symbol = chars.next()?;
    if chars.next()? != ']' || chars.next()? != ' ' {
        return None;
    }
    let text = chars.as_str().trim();
    Some(json!({
        "id": format!("note:{path}:{line_no}"),
        "sourcePath": path,
        "noteTitle": title,
        "line": line_no,
        "status": task_status(symbol),
        "title": strip_task_markers(text),
        "text": text,
        "dates": dates(text),
        "doneDate": marker_date(text, "✅").or_else(|| bracket_value(text, "done")),
        "dueDate": marker_date(text, "📅").or_else(|| bracket_value(text, "due")),
        "scheduledDate": marker_date(text, "⏳").or_else(|| bracket_value(text, "scheduled")),
        "recurrence": recurrence(text),
        "priority": task_priority(text),
        "tags": tags(text),
    }))
}

fn task_status(symbol: char) -> &'static str {
    match symbol {
        'x' | 'X' => "done",
        '/' => "in-progress",
        '-' => "cancelled",
        _ => "pending",
    }
}

fn dates(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_ascii_digit() && c != '-')
        .filter(|part| is_date(part))
        .map(ToString::to_string)
        .collect()
}

fn is_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 10 && bytes[4] == b'-' && bytes[7] == b'-'
}

fn marker_date(text: &str, marker: &str) -> Option<String> {
    let pos = text.find(marker)?;
    dates(&text[pos + marker.len()..]).into_iter().next()
}

fn bracket_value(text: &str, key: &str) -> Option<String> {
    let key = format!("[{key}::");
    let pos = text.find(&key)?;
    let rest = &text[pos + key.len()..];
    let end = rest.find(']')?;
    let value = rest[..end].trim();
    if value.is_empty() { None } else { Some(value.to_string()) }
}

fn recurrence(text: &str) -> Option<String> {
    if let Some(pos) = text.find('🔁') {
        return Some(text[pos + '🔁'.len_utf8()..].trim().to_string());
    }
    bracket_value(text, "repeat")
}

fn task_priority(text: &str) -> &'static str {
    if text.contains('🔺') || text.contains('⏫') || text.to_ascii_lowercase().contains("[priority:: high]") {
        "high"
    } else if text.contains('🔽') || text.contains('⏬') || text.to_ascii_lowercase().contains("[priority:: low]") {
        "low"
    } else {
        "medium"
    }
}

fn strip_task_markers(text: &str) -> String {
    let text = remove_bracket_fields(text);
    text.split_whitespace()
        .filter(|part| {
            !matches!(*part, "📅" | "⏳" | "✅" | "🔺" | "⏫" | "🔼" | "🔽" | "⏬" | "🔁")
                && !is_date(part)
                && !part.starts_with("[due::")
                && !part.starts_with("[scheduled::")
                && !part.starts_with("[priority::")
                && !part.starts_with("[repeat::")
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn remove_bracket_fields(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    loop {
        let Some(start) = rest.find('[') else {
            out.push_str(rest);
            break;
        };
        out.push_str(&rest[..start]);
        let after = &rest[start..];
        let lower = after.to_ascii_lowercase();
        if lower.starts_with("[due::") || lower.starts_with("[scheduled::") || lower.starts_with("[priority::") || lower.starts_with("[repeat::") {
            if let Some(end) = after.find(']') {
                rest = &after[end + 1..];
                continue;
            }
        }
        out.push('[');
        rest = &after[1..];
    }
    out
}

fn handle_search(state: &ApiState, query: &BTreeMap<String, String>) -> Response {
    let q = query.get("q").map(|value| value.trim().to_ascii_lowercase()).unwrap_or_default();
    let mut results = Vec::new();
    if !q.is_empty() {
        for (entry, content) in state_note_pairs(state) {
            let hay = format!("{}\n{}", entry["path"].as_str().unwrap_or(""), content).to_ascii_lowercase();
            if hay.contains(&q) {
                let content = content.as_str();
                let content_match = content.to_ascii_lowercase().find(&q);
                let (start, end) = if let Some(content_at) = content_match {
                    (content_at.saturating_sub(120).min(content.len()), (content_at + q.len() + 180).min(content.len()))
                } else {
                    (0, (q.len() + 179).min(content.len()))
                };
                let mut object = entry.as_object().cloned().unwrap_or_default();
                object.insert("excerpt".to_string(), Value::String(content[start..end].to_string()));
                results.push(Value::Object(object));
            }
        }
    }
    Response::json(200, json!({ "query": q, "results": results }))
}

fn handle_backlinks(state: &ApiState, query: &BTreeMap<String, String>) -> Response {
    let Some(note_path) = query.get("path") else { return Response::error(400, "invalid path") };
    let rel = if state.db.is_some() {
        note_path.trim_start_matches('/').to_string()
    } else {
        let Some(fp) = vault_path(&state.config, note_path) else { return Response::error(400, "invalid path") };
        fp.strip_prefix(&state.config.vault_dir).unwrap_or(&fp).to_string_lossy().replace('\\', "/")
    };
    let index = build_vault_index(state);
    let backlinks = index["backlinks"].as_object().and_then(|map| map.get(&rel)).cloned().unwrap_or_else(|| json!([]));
    Response::json(200, json!({ "path": rel, "backlinks": backlinks }))
}

fn handle_vault_asset(state: &ApiState, query: &BTreeMap<String, String>) -> Response {
    let Some(asset_path) = query.get("path") else { return Response::new(404, "{\"error\":\"asset not found\"}", "application/json") };
    let Some(fp) = vault_path(&state.config, asset_path) else { return Response::new(404, "{\"error\":\"asset not found\"}", "application/json") };
    if !fp.is_file() || !is_asset_path(&fp) {
        return Response::new(404, "{\"error\":\"asset not found\"}", "application/json");
    }
    Response::new(200, fs::read(&fp).unwrap_or_default(), mime_for(&fp))
}

fn export_ai(state: &ApiState) -> Value {
    let notes: Vec<Value> = state_note_pairs(state)
        .into_iter()
        .filter_map(|(entry, content)| {
            let mut object = entry.as_object()?.clone();
            object.insert("content".to_string(), Value::String(content));
            Some(Value::Object(object))
        })
        .collect();
    json!({ "schema": "copal.ai-export.v0", "exportedAt": Utc::now().to_rfc3339(), "vaultPath": vault_label(state), "notes": notes })
}

fn export_okf(state: &ApiState) -> Value {
    let index = build_vault_index(state);
    let label = vault_label(state);
    json!({
        "schema": "copal.okf-inspired.v0",
        "exportedAt": Utc::now().to_rfc3339(),
        "catalog": { "name": Path::new(&label).file_name().and_then(|value| value.to_str()).unwrap_or("vault"), "path": label },
        "resources": index["notes"].clone(),
        "relationships": index["graph"]["edges"].clone(),
        "tasks": index["tasks"].clone(),
    })
}

fn export_doclang(state: &ApiState) -> Vec<u8> {
    let mut parts = vec![
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>".to_string(),
        "<doclang>".to_string(),
        "  <head>".to_string(),
        "    <label>Copal Vault Draft Export</label>".to_string(),
        format!("    <description>Draft DocLang-style export from {}</description>", xml_escape(&vault_label(state))),
        "  </head>".to_string(),
    ];
    for (entry, content) in state_note_pairs(state) {
        let path = entry["path"].as_str().unwrap_or("");
        let title = note_title(path, &content);
        let cdata = content.replace("]]>", "]]]]><![CDATA[>");
        parts.push("  <section>".to_string());
        parts.push(format!("    <label>{}</label>", xml_escape(&title)));
        parts.push(format!("    <custom><path>{}</path><suffix>{}</suffix></custom>", xml_escape(path), xml_escape(entry["suffix"].as_str().unwrap_or(""))));
        parts.push(format!("    <content><![CDATA[{cdata}]]></content>"));
        parts.push("  </section>".to_string());
    }
    parts.push("</doclang>".to_string());
    parts.join("\n").into_bytes()
}

fn xml_escape(value: &str) -> String {
    value.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
}

fn export_markdown_bundle(state: &ApiState) -> Vec<u8> {
    let cursor = Cursor::new(Vec::new());
    let mut zip = zip::ZipWriter::new(cursor);
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    for (entry, content) in state_note_pairs(state) {
        let path = entry["path"].as_str().unwrap_or("");
        if !is_text_export_path(Path::new(path)) {
            continue;
        }
        if zip.start_file(path, options).is_ok() {
            let _ = zip.write_all(content.as_bytes());
        }
    }
    zip.finish().map(|cursor| cursor.into_inner()).unwrap_or_default()
}

fn serve_static(_config: &NativeApiConfig, path: &str) -> Response {
    #[cfg(feature = "embedded-assets")]
    {
        return serve_embedded_static(path);
    }

    #[cfg(not(feature = "embedded-assets"))]
    {
    let fp = safe_static_path(_config, path).unwrap_or_else(|| _config.site_dir.join("index.html"));
    if !fp.is_file() {
        return Response::new(404, "Build not found. Run: bun install && bun run build", "text/plain; charset=utf-8");
    }
    Response::new(200, fs::read(&fp).unwrap_or_default(), mime_for(&fp))
    }
}

#[allow(dead_code)]
fn safe_static_path(config: &NativeApiConfig, path: &str) -> Option<PathBuf> {
    let clean = if path == "/" || path.is_empty() { "index.html" } else { path.trim_start_matches('/') };
    let mut candidate = config.site_dir.clone();
    for part in clean.split('/') {
        if part.is_empty() {
            continue;
        }
        let part_path = Path::new(part);
        if part_path.components().any(|component| !matches!(component, Component::Normal(_))) {
            return None;
        }
        candidate.push(part);
    }
    if candidate.is_dir() {
        candidate.push("index.html");
    }
    if path_is_under(&candidate, &config.site_dir) && candidate.is_file() { Some(candidate) } else { None }
}

#[cfg(feature = "embedded-assets")]
fn serve_embedded_static(path: &str) -> Response {
    let clean = clean_static_request_path(path);
    let file = EMBEDDED_OUT
        .get_file(&clean)
        .or_else(|| {
            if clean.ends_with('/') {
                EMBEDDED_OUT.get_file(format!("{clean}index.html"))
            } else {
                None
            }
        })
        .or_else(|| EMBEDDED_OUT.get_file("index.html"));
    let Some(file) = file else {
        return Response::new(404, "Embedded build not found. Run bun run build before compiling embedded assets.", "text/plain; charset=utf-8");
    };
    Response::new(200, file.contents().to_vec(), mime_for(Path::new(file.path())))
}

#[cfg(feature = "embedded-assets")]
fn clean_static_request_path(path: &str) -> String {
    let raw = if path == "/" || path.is_empty() { "index.html" } else { path.trim_start_matches('/') };
    let mut parts = Vec::new();
    for part in raw.split('/') {
        if part.is_empty() || part == "." || part == ".." {
            continue;
        }
        parts.push(part);
    }
    parts.join("/")
}

fn mime_for(path: &Path) -> &'static str {
    match suffix(path).as_str() {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "application/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" | "map" => "application/json; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "ico" => "image/x-icon",
        "webp" => "image/webp",
        "woff2" => "font/woff2",
        "woff" => "font/woff",
        "ttf" => "font/ttf",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_config() -> NativeApiConfig {
        let root = std::env::temp_dir().join(format!("copal-native-api-test-{}", now_stamp()));
        let site = root.join("out");
        let vault = root.join("vault");
        fs::create_dir_all(site.join("data")).unwrap();
        fs::create_dir_all(&vault).unwrap();
        fs::write(site.join("index.html"), "<html>Copal</html>").unwrap();
        fs::write(site.join("app.js"), "console.log('x')").unwrap();
        fs::write(site.join("data").join("move-data.json"), "{\"seed\":true}").unwrap();
        fs::write(vault.join("Welcome.md"), "# Welcome\nSee [[Other]]. #tag\n- [ ] Task 📅 2026-07-15 🔺\n").unwrap();
        fs::write(vault.join("Other.md"), "# Other\n").unwrap();
        fs::write(vault.join("image.png"), b"png").unwrap();
        NativeApiConfig { root_dir: root.clone(), site_dir: site, data_file: root.join("move-data.json"), seed_file: root.join("out/data/move-data.json"), vault_dir: vault }
    }

    fn files_state(config: NativeApiConfig) -> ApiState {
        ApiState { config, write_lock: Mutex::new(()), db: None, events: db_api::Broadcaster::new() }
    }

    #[test]
    fn path_traversal_rejected() {
        let config = temp_config();
        assert!(vault_path(&config, "../x.md").is_none());
        assert!(vault_path(&config, "safe/../../x.md").is_none());
        assert!(safe_static_path(&config, "/../Cargo.toml").is_none());
    }

    #[test]
    fn note_save_backup_and_asset_allowlist() {
        let config = temp_config();
        let state = files_state(config.clone());
        let response = handle_note_post(&state, br##"{"path":"Welcome.md","content":"# Changed"}"##);
        assert_eq!(response.code, 200);
        assert!(String::from_utf8(response.body).unwrap().contains(".copal/backups/"));
        assert_eq!(fs::read_to_string(config.vault_dir.join("Welcome.md")).unwrap(), "# Changed");

        let ok = handle_vault_asset(&state, &BTreeMap::from([("path".to_string(), "image.png".to_string())]));
        assert_eq!(ok.code, 200);
        let bad = handle_vault_asset(&state, &BTreeMap::from([("path".to_string(), "Welcome.md".to_string())]));
        assert_eq!(bad.code, 404);
    }

    #[test]
    fn exports_and_index_work() {
        let state = files_state(temp_config());
        let index = build_vault_index(&state);
        assert!(index["tasks"].as_array().unwrap().len() == 1);
        assert!(index["graph"]["edges"].as_array().unwrap().iter().any(|edge| edge["type"] == "wikilink"));
        assert_eq!(export_ai(&state)["schema"], "copal.ai-export.v0");
        assert_eq!(export_okf(&state)["schema"], "copal.okf-inspired.v0");
        let doclang = String::from_utf8(export_doclang(&state)).unwrap();
        assert!(doclang.contains("<doclang>"));
        let bundle = export_markdown_bundle(&state);
        assert!(!bundle.is_empty());
    }

    #[test]
    fn db_mode_adapters_and_events() {
        let config = temp_config();
        let data_dir = config.root_dir.join("dbdata");
        let db = copal_db::Db::open(&data_dir).unwrap();
        db.import_vault(&config.vault_dir, None).unwrap();
        let state = ApiState { config, write_lock: Mutex::new(()), db: Some(db), events: db_api::Broadcaster::new() };

        // Legacy endpoints run over the DB.
        let index = build_vault_index(&state);
        assert_eq!(index["tasks"].as_array().unwrap().len(), 1);
        let response = handle_request(&state, Request { method: "GET".into(), target: "/api/note?path=Welcome.md".into(), body: Vec::new() });
        assert_eq!(response.code, 200);
        let body: Value = serde_json::from_slice(&response.body).unwrap();
        assert!(body["content"].as_str().unwrap().contains("# Welcome"));

        // Write through the legacy adapter → new head commit + history.
        let response = handle_request(&state, Request { method: "POST".into(), target: "/api/note".into(), body: br##"{"path":"Welcome.md","content":"# Changed"}"##.to_vec() });
        assert_eq!(response.code, 200);
        let doc_id = serde_json::from_slice::<Value>(&response.body).unwrap()["docId"].as_str().unwrap().to_string();
        let history = handle_request(&state, Request { method: "GET".into(), target: format!("/api/doc/history?id={doc_id}"), body: Vec::new() });
        assert_eq!(history.code, 200);

        // DB-native endpoints.
        let docs = handle_request(&state, Request { method: "GET".into(), target: "/api/docs".into(), body: Vec::new() });
        assert_eq!(docs.code, 200);
        let ops = handle_request(&state, Request { method: "GET".into(), target: "/api/ops".into(), body: Vec::new() });
        assert_eq!(ops.code, 200);

        // Stale write is rejected with the authoritative doc.
        let stale = handle_request(&state, Request {
            method: "POST".into(),
            target: "/api/doc".into(),
            body: format!(r#"{{"id":"{doc_id}","content":"clobber","baseCommit":"bogus"}}"#).into_bytes(),
        });
        assert_eq!(stale.code, 409);

        // Undo restores the pre-write content.
        let undo = handle_request(&state, Request { method: "POST".into(), target: "/api/undo".into(), body: b"{}".to_vec() });
        assert_eq!(undo.code, 200);
        let response = handle_request(&state, Request { method: "GET".into(), target: "/api/note?path=Welcome.md".into(), body: Vec::new() });
        let body: Value = serde_json::from_slice(&response.body).unwrap();
        assert!(body["content"].as_str().unwrap().contains("# Welcome"));
    }

    #[test]
    fn ui_state_merge_and_static_mime() {
        let config = temp_config();
        let state = files_state(config.clone());
        let response = handle_ui_state_post(&state, br#"{"active":"wiki"}"#);
        assert_eq!(response.code, 200);
        assert_eq!(read_ui_state(&config)["active"], "wiki");
        #[cfg(not(feature = "embedded-assets"))]
        {
            let static_response = serve_static(&config, "/app.js");
            assert_eq!(static_response.ctype, "application/javascript; charset=utf-8");
        }
        #[cfg(feature = "embedded-assets")]
        {
            let static_response = serve_static(&config, "/");
            assert_eq!(static_response.ctype, "text/html; charset=utf-8");
            assert!(!static_response.body.is_empty());
        }
    }
}
