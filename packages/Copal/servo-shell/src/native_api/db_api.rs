//! DB-backed API surface + the one data stream.
//!
//! Everything here rides on `copal-db` (see
//! `.futures/copal-jj-db-source-of-truth-metaplan.md`): docs/commits/ops
//! endpoints, the `/api/events` SSE stream (the commit landing IS the sync
//! event), and DB-mode adapters for the legacy vault endpoints so the
//! existing UI keeps working over the database unchanged.

use std::collections::BTreeMap;
use std::io::Write;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use copal_db::{Content, Db, DocView, WriteOutcome};
use serde_json::{json, Value};

use super::{mime_for, read_json_object, ApiState, Response};

// ── SSE broadcaster: the one data stream ─────────────────────────────────

pub(crate) struct Broadcaster {
    clients: Mutex<Vec<TcpStream>>,
}

impl Broadcaster {
    pub(crate) fn new() -> Self {
        Self { clients: Mutex::new(Vec::new()) }
    }

    /// Take over a connection as an SSE subscriber. The stream stays open;
    /// events are pushed by `broadcast` until the client goes away.
    pub(crate) fn subscribe(&self, mut stream: TcpStream) -> std::io::Result<()> {
        stream.set_write_timeout(Some(Duration::from_secs(1)))?;
        stream.write_all(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: keep-alive\r\n\r\n",
        )?;
        stream.write_all(b"event: hello\ndata: {}\n\n")?;
        stream.flush()?;
        self.clients.lock().unwrap().push(stream);
        Ok(())
    }

    pub(crate) fn broadcast(&self, event: &str, data: &Value) {
        let payload = format!("event: {event}\ndata: {}\n\n", data);
        let mut clients = self.clients.lock().unwrap();
        clients.retain_mut(|client| client.write_all(payload.as_bytes()).and_then(|_| client.flush()).is_ok());
    }
}

fn doc_json(view: &DocView) -> Value {
    serde_json::to_value(view).unwrap_or_else(|_| json!({}))
}

/// `doc-changed`: a commit landed for this doc. Includes the content so
/// subscribed tabs can apply it directly; `client` lets the originator skip
/// its own echo.
pub(crate) fn emit_doc(state: &ApiState, view: &DocView, client: Option<&str>) {
    let mut data = doc_json(view);
    if let Some(object) = data.as_object_mut() {
        object.insert("client".to_string(), client.map(|c| json!(c)).unwrap_or(Value::Null));
    }
    state.events.broadcast("doc-changed", &data);
    state.events.broadcast("index-changed", &json!({}));
}

pub(crate) fn emit_doc_deleted(state: &ApiState, doc_id: &str) {
    state.events.broadcast("doc-changed", &json!({ "id": doc_id, "deleted": true }));
    state.events.broadcast("index-changed", &json!({}));
}

pub(crate) fn emit_view(state: &ApiState, kind: &str, description: &str) {
    state.events.broadcast("view-changed", &json!({ "kind": kind, "description": description }));
}

// ── New DB endpoints ─────────────────────────────────────────────────────

/// Handle the DB-native routes. Returns None for routes owned elsewhere.
pub(crate) fn handle(
    state: &ApiState,
    method: &str,
    path: &str,
    query: &BTreeMap<String, String>,
    body: &[u8],
) -> Option<Response> {
    if !path.starts_with("/api/doc")
        && !matches!(path, "/api/docs" | "/api/ops" | "/api/undo" | "/api/import/vault" | "/api/asset")
    {
        return None;
    }
    let Some(db) = state.db.as_ref() else {
        return Some(Response::error(503, "database unavailable (COPAL_SOURCE=files)"));
    };
    let response = match (method, path) {
        ("GET" | "HEAD", "/api/docs") => list_docs(db),
        ("GET" | "HEAD", "/api/doc") => get_doc(db, query),
        ("POST", "/api/doc") => post_doc(state, db, body),
        ("POST", "/api/doc/checkpoint") => checkpoint(state, db, body),
        ("GET" | "HEAD", "/api/doc/history") => history(db, query),
        ("GET" | "HEAD", "/api/doc/diff") => diff(db, query),
        ("POST", "/api/doc/restore") => restore(state, db, body),
        ("POST", "/api/doc/rename") => rename(state, db, body),
        ("POST", "/api/doc/delete") => delete(state, db, body),
        ("GET" | "HEAD", "/api/ops") => ops(db, query),
        ("POST", "/api/undo") => undo(state, db, body),
        ("POST", "/api/import/vault") => import_vault(state, db, body),
        ("GET" | "HEAD", "/api/asset") => asset(db, query),
        _ => Response::error(404, "not found"),
    };
    Some(response)
}

fn list_docs(db: &Db) -> Response {
    match db.list_docs() {
        Ok(docs) => Response::json(200, json!({ "docs": docs.iter().map(doc_json).collect::<Vec<_>>() })),
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn get_doc(db: &Db, query: &BTreeMap<String, String>) -> Response {
    let Some(id) = query.get("id") else { return Response::error(400, "missing id") };
    match db.get_doc(id) {
        Ok(Some(view)) => Response::json(200, doc_json(&view)),
        Ok(None) => Response::error(404, "doc not found"),
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn post_doc(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let content = data.get("content").and_then(Value::as_str).unwrap_or("");
    let client = data.get("client").and_then(Value::as_str);
    match data.get("id").and_then(Value::as_str) {
        Some(id) => {
            let base = data.get("baseCommit").and_then(Value::as_str);
            match db.write_doc(id, content, base) {
                Ok(WriteOutcome::Committed { view, new_change }) => {
                    emit_doc(state, &view, client);
                    Response::json(200, json!({ "ok": true, "outcome": "committed", "newChange": new_change, "doc": doc_json(&view) }))
                },
                Ok(WriteOutcome::Unchanged { view }) => {
                    Response::json(200, json!({ "ok": true, "outcome": "unchanged", "doc": doc_json(&view) }))
                },
                Ok(WriteOutcome::Stale { view }) => {
                    Response::json(409, json!({ "ok": false, "outcome": "stale", "doc": doc_json(&view) }))
                },
                Err(error) => Response::error(400, &error.to_string()),
            }
        },
        None => {
            let Some(name) = data.get("name").and_then(Value::as_str) else {
                return Response::error(400, "need id or name");
            };
            let kind = data.get("kind").and_then(Value::as_str).unwrap_or_else(|| kind_for_name(name));
            match db.create_doc(kind, name, content, None) {
                Ok(view) => {
                    emit_doc(state, &view, client);
                    Response::json(200, json!({ "ok": true, "outcome": "created", "doc": doc_json(&view) }))
                },
                Err(error) => Response::error(409, &error.to_string()),
            }
        },
    }
}

fn checkpoint(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let Some(id) = data.get("id").and_then(Value::as_str) else { return Response::error(400, "missing id") };
    let message = data.get("message").and_then(Value::as_str);
    match db.checkpoint(id, message) {
        Ok(view) => {
            emit_view(state, "checkpoint", &format!("checkpoint {}", view.name));
            Response::json(200, json!({ "ok": true, "doc": doc_json(&view) }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn history(db: &Db, query: &BTreeMap<String, String>) -> Response {
    let Some(id) = query.get("id") else { return Response::error(400, "missing id") };
    match db.history(id) {
        Ok(value) => Response::json(200, value),
        Err(error) => Response::error(404, &error.to_string()),
    }
}

fn diff(db: &Db, query: &BTreeMap<String, String>) -> Response {
    let (Some(from), Some(to)) = (query.get("from"), query.get("to")) else {
        return Response::error(400, "missing from/to");
    };
    match db.diff(from, to) {
        Ok(diff) => Response::json(200, json!({ "from": from, "to": to, "diff": diff })),
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn restore(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let (Some(id), Some(commit)) = (data.get("id").and_then(Value::as_str), data.get("commit").and_then(Value::as_str)) else {
        return Response::error(400, "missing id/commit");
    };
    match db.restore_doc(id, commit) {
        Ok(view) => {
            emit_doc(state, &view, None);
            emit_view(state, "restore", &format!("restore {}", view.name));
            Response::json(200, json!({ "ok": true, "doc": doc_json(&view) }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn rename(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let (Some(id), Some(name)) = (data.get("id").and_then(Value::as_str), data.get("name").and_then(Value::as_str)) else {
        return Response::error(400, "missing id/name");
    };
    match db.rename_doc(id, name) {
        Ok(view) => {
            emit_doc(state, &view, None);
            emit_view(state, "rename", &format!("rename to {}", view.name));
            Response::json(200, json!({ "ok": true, "doc": doc_json(&view) }))
        },
        Err(error) => Response::error(409, &error.to_string()),
    }
}

fn delete(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let Some(id) = data.get("id").and_then(Value::as_str) else { return Response::error(400, "missing id") };
    match db.delete_doc(id) {
        Ok(()) => {
            emit_doc_deleted(state, id);
            emit_view(state, "delete", "delete doc");
            Response::json(200, json!({ "ok": true }))
        },
        Err(error) => Response::error(404, &error.to_string()),
    }
}

fn ops(db: &Db, query: &BTreeMap<String, String>) -> Response {
    let limit = query.get("limit").and_then(|value| value.parse().ok()).unwrap_or(50).min(500);
    let before = query.get("before").map(String::as_str);
    match db.ops(limit, before) {
        Ok(value) => Response::json(200, value),
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn undo(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = read_json_object(body).unwrap_or_default();
    let target = data.get("op").and_then(Value::as_str);
    match db.undo(target) {
        Ok(changed) => {
            for doc_id in &changed {
                match db.get_doc(doc_id) {
                    Ok(Some(view)) => emit_doc(state, &view, None),
                    _ => emit_doc_deleted(state, doc_id),
                }
            }
            emit_view(state, "undo", "undo");
            Response::json(200, json!({ "ok": true, "changedDocs": changed }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn import_vault(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = read_json_object(body).unwrap_or_default();
    let dir = data
        .get("path")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .unwrap_or_else(|| state.config.vault_dir.clone());
    if !dir.is_dir() {
        return Response::error(400, "vault path is not a directory");
    }
    let planning = state.config.data_file.exists().then(|| state.config.data_file.clone());
    match db.import_vault(&dir, planning.as_deref()) {
        Ok(stats) => {
            emit_view(state, "import", &format!("import {} notes, {} assets", stats.notes, stats.assets));
            state.events.broadcast("index-changed", &json!({}));
            Response::json(200, json!({ "ok": true, "stats": stats }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

fn asset(db: &Db, query: &BTreeMap<String, String>) -> Response {
    let view = if let Some(id) = query.get("id") {
        db.get_doc(id).ok().flatten()
    } else if let Some(name) = query.get("name") {
        db.find_doc_by_name(name).ok().flatten()
    } else {
        None
    };
    let Some(view) = view else { return Response::error(404, "asset not found") };
    let Content::Asset { hash, ext, .. } = &view.content else { return Response::error(404, "not an asset") };
    let file = db.asset_file(hash, ext);
    match std::fs::read(&file) {
        Ok(bytes) => Response::new(200, bytes, mime_for(Path::new(&file))),
        Err(_) => Response::error(404, "asset file missing"),
    }
}

// ── Legacy vault-endpoint adapters (same UI, DB underneath) ──────────────

fn kind_for_name(name: &str) -> &'static str {
    match Path::new(name).extension().and_then(|value| value.to_str()).unwrap_or("").to_ascii_lowercase().as_str() {
        "base" => "base",
        "canvas" => "canvas",
        _ => "markdown",
    }
}

const NOTE_KINDS: &[&str] = &["markdown", "base", "canvas"];

fn suffix_of(name: &str) -> String {
    format!(".{}", Path::new(name).extension().and_then(|value| value.to_str()).unwrap_or("").to_ascii_lowercase())
}

/// (entry-metadata, content) pairs shaped exactly like the file-based
/// index expects — the bridge that lets `build_vault_index`, search and the
/// exporters run over the DB unchanged.
pub(crate) fn note_pairs(db: &Db) -> Vec<(Value, String)> {
    let Ok(docs) = db.list_docs() else { return Vec::new() };
    docs.into_iter()
        .filter(|doc| NOTE_KINDS.contains(&doc.kind.as_str()))
        .filter_map(|doc| {
            let content = doc.text.clone()?;
            let entry = json!({
                "path": doc.name,
                "name": Path::new(&doc.name).file_name().and_then(|value| value.to_str()).unwrap_or(&doc.name),
                "suffix": suffix_of(&doc.name),
                "size": content.len(),
                "mtime": doc.ts as f64 / 1000.0,
                "docId": doc.id,
                "head": doc.head,
            });
            Some((entry, content))
        })
        .collect()
}

pub(crate) fn legacy_notes(db: &Db, vault_label: &str) -> Response {
    let notes: Vec<Value> = note_pairs(db).into_iter().map(|(entry, _)| entry).collect();
    Response::json(200, json!({ "vaultPath": vault_label, "notes": notes }))
}

pub(crate) fn legacy_note_get(db: &Db, query: &BTreeMap<String, String>) -> Response {
    let Some(path) = query.get("path") else { return Response::error(404, "note not found") };
    match db.find_doc_by_name(path) {
        Ok(Some(view)) if NOTE_KINDS.contains(&view.kind.as_str()) => Response::json(200, json!({
            "path": view.name,
            "name": Path::new(&view.name).file_name().and_then(|value| value.to_str()).unwrap_or(&view.name),
            "suffix": suffix_of(&view.name),
            "content": view.text.clone().unwrap_or_default(),
            "mtime": view.ts as f64 / 1000.0,
            "docId": view.id,
            "head": view.head,
        })),
        _ => Response::error(404, "note not found"),
    }
}

pub(crate) fn legacy_note_post(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let path = data.get("path").and_then(Value::as_str).unwrap_or("");
    let content = data.get("content").and_then(Value::as_str).unwrap_or("");
    if path.is_empty() || !NOTE_KINDS.contains(&kind_for_name(path)) {
        return Response::error(400, "invalid note path");
    }
    let result = match db.find_doc_by_name(path) {
        Ok(Some(existing)) => db.write_doc(&existing.id, content, None).map(|outcome| match outcome {
            WriteOutcome::Committed { view, .. } | WriteOutcome::Unchanged { view } | WriteOutcome::Stale { view } => view,
        }),
        Ok(None) => db.create_doc(kind_for_name(path), path, content, None),
        Err(error) => Err(error),
    };
    match result {
        Ok(view) => {
            emit_doc(state, &view, None);
            Response::json(200, json!({ "ok": true, "path": view.name, "backup": Value::Null, "docId": view.id }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

pub(crate) fn legacy_note_rename(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let src = data.get("path").and_then(Value::as_str).unwrap_or("");
    let dst = data.get("newPath").and_then(Value::as_str).unwrap_or("");
    let Ok(Some(view)) = db.find_doc_by_name(src) else { return Response::error(404, "source note not found") };
    if db.find_doc_by_name(dst).ok().flatten().is_some() {
        return Response::error(409, "destination exists");
    }
    match db.rename_doc(&view.id, dst) {
        Ok(renamed) => {
            emit_doc(state, &renamed, None);
            Response::json(200, json!({ "ok": true, "path": renamed.name, "backup": Value::Null }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

pub(crate) fn legacy_note_delete(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    let data = match read_json_object(body) {
        Ok(data) => data,
        Err(error) => return Response::error(400, &error),
    };
    let path = data.get("path").and_then(Value::as_str).unwrap_or("");
    let Ok(Some(view)) = db.find_doc_by_name(path) else { return Response::error(404, "note not found") };
    match db.delete_doc(&view.id) {
        Ok(()) => {
            emit_doc_deleted(state, &view.id);
            Response::json(200, json!({ "ok": true, "backup": Value::Null }))
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}

pub(crate) fn legacy_vault_asset(db: &Db, query: &BTreeMap<String, String>) -> Option<Response> {
    let name = query.get("path")?;
    let view = db.find_doc_by_name(name).ok().flatten()?;
    let Content::Asset { hash, ext, .. } = &view.content else { return None };
    let file = db.asset_file(hash, ext);
    let bytes = std::fs::read(&file).ok()?;
    Some(Response::new(200, bytes, mime_for(Path::new(&file))))
}

// ── Planning doc (move-data.json) adapters ───────────────────────────────

pub(crate) const PLANNING_DOC_NAME: &str = "move-data.json";

pub(crate) fn planning_get(db: &Db) -> Option<Vec<u8>> {
    let view = db.find_doc_by_name(PLANNING_DOC_NAME).ok().flatten()?;
    if view.kind != "planning" {
        return None;
    }
    view.text.map(String::into_bytes)
}

pub(crate) fn planning_post(state: &ApiState, db: &Db, body: &[u8]) -> Response {
    if serde_json::from_slice::<Value>(body).is_err() {
        return Response::new(400, "{\"error\":\"invalid json\"}", "application/json");
    }
    let content = String::from_utf8_lossy(body).into_owned();
    let result = match db.find_doc_by_name(PLANNING_DOC_NAME) {
        Ok(Some(existing)) => db.write_doc(&existing.id, &content, None).map(|outcome| match outcome {
            WriteOutcome::Committed { view, .. } | WriteOutcome::Unchanged { view } | WriteOutcome::Stale { view } => view,
        }),
        Ok(None) => db.create_doc("planning", PLANNING_DOC_NAME, &content, None),
        Err(error) => Err(error),
    };
    match result {
        Ok(view) => {
            emit_doc(state, &view, None);
            Response::new(200, "{\"ok\":true}", "application/json")
        },
        Err(error) => Response::error(400, &error.to_string()),
    }
}
