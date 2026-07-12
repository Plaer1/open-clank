use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, Write};
use std::path::PathBuf;

use copal_db::{Content, Db, DocView, WriteOutcome};
use serde_json::{json, Value};

fn required<'a>(args: &'a Value, key: &str) -> Result<&'a str, String> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("missing {key}"))
}

fn optional<'a>(args: &'a Value, key: &str) -> Option<&'a str> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn scope<'a>(args: &'a Value) -> Result<(&'a str, &'a str), String> {
    Ok((required(args, "owner")?, required(args, "workspace_id")?))
}

fn links(text: &str) -> Vec<String> {
    let mut found = BTreeSet::new();
    let mut rest = text;
    while let Some(start) = rest.find("[[") {
        rest = &rest[start + 2..];
        let Some(end) = rest.find("]]") else { break };
        let target = rest[..end].split(['|', '#']).next().unwrap_or("").trim();
        if !target.is_empty() {
            found.insert(target.to_string());
        }
        rest = &rest[end + 2..];
    }
    found.into_iter().collect()
}

fn tags(text: &str) -> Vec<String> {
    let mut found = BTreeSet::new();
    for word in text.split_whitespace() {
        let tag = word
            .strip_prefix('#')
            .unwrap_or("")
            .trim_matches(|ch: char| !ch.is_alphanumeric() && ch != '-' && ch != '_' && ch != '/');
        if !tag.is_empty() && !tag.chars().all(|ch| ch.is_ascii_digit()) {
            found.insert(tag.to_string());
        }
    }
    found.into_iter().collect()
}

fn frontmatter(text: &str) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    let Some(body) = text.strip_prefix("---\n") else {
        return values;
    };
    let Some(end) = body.find("\n---") else {
        return values;
    };
    for line in body[..end].lines() {
        if let Some((key, value)) = line.split_once(':') {
            values.insert(
                key.trim().to_string(),
                value.trim().trim_matches(['\'', '"']).to_string(),
            );
        }
    }
    values
}

fn tasks(text: &str, document_id: &str) -> Vec<Value> {
    text.lines()
        .enumerate()
        .filter_map(|(index, line)| {
            let trimmed = line.trim_start();
            let (done, value) = if let Some(value) = trimmed.strip_prefix("- [ ] ") {
                (false, value)
            } else if let Some(value) = trimmed
                .strip_prefix("- [x] ")
                .or_else(|| trimmed.strip_prefix("- [X] "))
            {
                (true, value)
            } else {
                return None;
            };
            Some(json!({
                "id": format!("{document_id}:{}", index + 1),
                "line": index + 1,
                "done": done,
                "text": value,
            }))
        })
        .collect()
}

fn indexed(doc: &DocView) -> Value {
    let text = doc.text.as_deref().unwrap_or("");
    let frontmatter = frontmatter(text);
    let tasks = tasks(text, &doc.id);
    let course = frontmatter.get("course").cloned();
    let skill = frontmatter.get("skill").cloned();
    let treehouse = if course.is_some() || skill.is_some() || doc.kind.starts_with("treehouse-") {
        Some(json!({
            "id": doc.id,
            "course": course,
            "skill": skill,
            "prerequisite": frontmatter.get("depends_on"),
            "evidence_task_ids": tasks.iter().filter_map(|task| task.get("id")).collect::<Vec<_>>(),
            "source_document_id": doc.id,
            "source_head": doc.head,
        }))
    } else {
        None
    };
    json!({
        "id": doc.id,
        "kind": doc.kind,
        "owner": doc.owner,
        "workspace_id": doc.workspace_id,
        "name": doc.name,
        "head": doc.head,
        "ts": doc.ts,
        "text": doc.text,
        "content": doc.content,
        "links": links(text),
        "tags": tags(text),
        "frontmatter": frontmatter,
        "tasks": tasks,
        "treehouse": treehouse,
    })
}

fn outcome(value: WriteOutcome) -> Value {
    match value {
        WriteOutcome::Committed { view, new_change } => {
            json!({ "outcome": "committed", "new_change": new_change, "doc": view })
        }
        WriteOutcome::Unchanged { view } => json!({ "outcome": "unchanged", "doc": view }),
        WriteOutcome::Stale { view } => json!({ "outcome": "stale", "doc": view }),
    }
}

fn execute(db: &Db, op: &str, args: &Value) -> Result<Value, String> {
    match op {
        "status" => {
            let docs = db.list_docs().map_err(|error| error.to_string())?;
            let mut kinds = BTreeMap::<String, usize>::new();
            for doc in &docs {
                *kinds.entry(doc.kind.clone()).or_default() += 1;
            }
            Ok(json!({
                "schema_version": db.schema_version().map_err(|error| error.to_string())?,
                "documents": docs.len(),
                "kinds": kinds,
                "integrity_ok": true,
            }))
        }
        "import_vault" => {
            let (owner, workspace_id) = scope(args)?;
            let vault = PathBuf::from(required(args, "path")?);
            let planning = optional(args, "planning_path").map(PathBuf::from);
            db.import_vault_scoped(&vault, planning.as_deref(), owner, workspace_id)
                .map(|stats| json!(stats))
                .map_err(|error| error.to_string())
        }
        "list" | "index" | "search" | "export_snapshot" => {
            let (owner, workspace_id) = scope(args)?;
            let query = optional(args, "query").unwrap_or("").to_lowercase();
            let kind = optional(args, "kind");
            let docs = db
                .list_docs_scoped(owner, workspace_id)
                .map_err(|error| error.to_string())?
                .into_iter()
                .filter(|doc| kind.is_none_or(|value| doc.kind == value))
                .filter(|doc| {
                    query.is_empty()
                        || doc.name.to_lowercase().contains(&query)
                        || doc
                            .text
                            .as_deref()
                            .unwrap_or("")
                            .to_lowercase()
                            .contains(&query)
                })
                .collect::<Vec<_>>();
            if op == "list" {
                return Ok(json!({ "docs": docs }));
            }
            Ok(json!({ "docs": docs.iter().map(indexed).collect::<Vec<_>>() }))
        }
        "get" => {
            let (owner, workspace_id) = scope(args)?;
            let doc = db
                .get_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())?
                .ok_or_else(|| "doc not found".to_string())?;
            Ok(indexed(&doc))
        }
        "trash" => {
            let (owner, workspace_id) = scope(args)?;
            let docs = db
                .list_deleted_docs_scoped(owner, workspace_id)
                .map_err(|error| error.to_string())?;
            Ok(json!({ "docs": docs }))
        }
        "create" => {
            let (owner, workspace_id) = scope(args)?;
            let doc = db
                .create_doc_scoped(
                    owner,
                    workspace_id,
                    optional(args, "kind").unwrap_or("markdown"),
                    required(args, "name")?,
                    args.get("content").and_then(Value::as_str).unwrap_or(""),
                    optional(args, "message"),
                )
                .map_err(|error| error.to_string())?;
            Ok(json!({ "outcome": "created", "doc": doc }))
        }
        "write" => {
            let (owner, workspace_id) = scope(args)?;
            db.write_doc_scoped(
                required(args, "id")?,
                args.get("content").and_then(Value::as_str).unwrap_or(""),
                optional(args, "base"),
                owner,
                workspace_id,
            )
            .map(outcome)
            .map_err(|error| error.to_string())
        }
        "history" => {
            let (owner, workspace_id) = scope(args)?;
            db.history_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())
        }
        "checkpoint" => {
            let (owner, workspace_id) = scope(args)?;
            db.checkpoint_scoped(
                required(args, "id")?,
                optional(args, "message"),
                owner,
                workspace_id,
            )
            .map(|doc| json!({ "doc": doc }))
            .map_err(|error| error.to_string())
        }
        "rename" => {
            let (owner, workspace_id) = scope(args)?;
            db.rename_doc_scoped(
                required(args, "id")?,
                required(args, "name")?,
                owner,
                workspace_id,
            )
            .map(|doc| json!({ "doc": doc }))
            .map_err(|error| error.to_string())
        }
        "delete" => {
            let (owner, workspace_id) = scope(args)?;
            db.delete_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map(|_| json!({ "deleted": true }))
                .map_err(|error| error.to_string())
        }
        "restore" => {
            let (owner, workspace_id) = scope(args)?;
            db.restore_doc_scoped(
                required(args, "id")?,
                required(args, "commit")?,
                owner,
                workspace_id,
            )
            .map(|doc| json!({ "doc": doc }))
            .map_err(|error| error.to_string())
        }
        "restore_deleted" => {
            let (owner, workspace_id) = scope(args)?;
            db.restore_deleted_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map(|doc| json!({ "doc": doc }))
                .map_err(|error| error.to_string())
        }
        "diff" => {
            let (owner, workspace_id) = scope(args)?;
            db.history_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())?;
            db.diff(required(args, "from")?, required(args, "to")?)
                .map(|diff| json!({ "diff": diff }))
                .map_err(|error| error.to_string())
        }
        "ops" => db
            .ops(
                args.get("limit")
                    .and_then(Value::as_u64)
                    .unwrap_or(50)
                    .min(500) as usize,
                optional(args, "before"),
            )
            .map_err(|error| error.to_string()),
        "asset_path" => {
            let (owner, workspace_id) = scope(args)?;
            let doc = db
                .get_doc_scoped(required(args, "id")?, owner, workspace_id)
                .map_err(|error| error.to_string())?
                .ok_or_else(|| "asset not found".to_string())?;
            let Content::Asset { hash, ext, size } = doc.content else {
                return Err("doc is not an asset".to_string());
            };
            Ok(json!({ "path": db.asset_file(&hash, &ext), "name": doc.name, "size": size }))
        }
        _ => Err(format!("unknown operation: {op}")),
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let data_dir = std::env::var_os("COPAL_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::args_os().nth(1).map(PathBuf::from))
        .ok_or("COPAL_DATA_DIR is required")?;
    let db = Db::open(&data_dir).map_err(|error| io::Error::other(error.to_string()))?;
    let stdin = io::stdin();
    let mut stdout = io::stdout().lock();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let request = serde_json::from_str::<Value>(&line);
        let id = request
            .as_ref()
            .ok()
            .and_then(|value| value.get("id"))
            .cloned()
            .unwrap_or(Value::Null);
        let response = match request {
            Ok(value) => {
                let op = value.get("op").and_then(Value::as_str).unwrap_or("");
                let args = value.get("args").unwrap_or(&Value::Null);
                match execute(&db, op, args) {
                    Ok(result) => json!({ "id": id, "ok": true, "result": result }),
                    Err(error) => json!({ "id": id, "ok": false, "error": error }),
                }
            }
            Err(error) => {
                json!({ "id": id, "ok": false, "error": format!("invalid request: {error}") })
            }
        };
        serde_json::to_writer(&mut stdout, &response)?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
    }
    Ok(())
}
