//! Authenticated mutation-owner adapter for the local history service.
//!
//! This module intentionally lives beside `FileEngine`: HTTP/UI callers cannot
//! bypass the preimage boundary. Capture failures are retained as per-action
//! status and never turn an otherwise successful filesystem mutation into a
//! false protected-history receipt.

use crate::engine::{WorktreeCaptureBinding,OrdinaryWriteCapture, OrdinaryCreateCapture};
use crate::{
    CaptureAfter, CaptureStatus, CaptureTarget, CheckpointBinding, CheckpointSelection, CheckpointTicket, MutationCaptureHook, MutationCaptureTicket,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

const INLINE_CONTENT_BYTES: usize = 640 * 1024;
const STAGE_CHUNK_BYTES: usize = 512 * 1024;

#[cfg(unix)]
use std::io::{BufRead, BufReader, Write};
#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[derive(Clone)]
pub struct HistoryServiceHook {
    mark_recovery: bool,
    socket_path: PathBuf,
    actor_id: String,
    account_id: String,
    workspace_id: String,
    authorized_roots: Vec<AuthorizedHistoryRoot>,
    token: String,
    checkpoint_tokens: HashMap<String, String>,
    worktree_tokens: HashMap<String, String>,
    actor_kind: String,
    workflow_writer: OnceLock<WorkflowWriterContext>,
    ordinary_writer: OnceLock<OrdinaryWriterContext>,
    graph_capture: OnceLock<GraphCaptureContext>,
    statuses: Arc<Mutex<HashMap<String, CaptureStatus>>>,
    sequence: Arc<Mutex<u64>>,
    last_action: Arc<Mutex<Option<String>>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct WorkflowWriterSource {
    session_id:String, actor_id:String, execution_revision:u64, root_operation_id:String, inner_turn_id:String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct WorkflowWriterContext {
    version:u8, account_id:String, session_id:String, actor_id:String, execution_revision:u64,
    root_operation_id:String, inner_turn_id:String, tool_call_id:String, source:WorkflowWriterSource,
    operation_id:String, request_digest:String, target_sha256:String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OrdinaryWriterOrigin {
    version:u8, host_chat_id:String, engine_session_id:String, root_operation_id:String,
    actor_id:String, execution_revision:u64, inner_turn_id:String, tool_call_id:String, assistant_message_id:String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OrdinaryWriterContext {
    version:u8, account_id:String, canonical_tool:String, operation_id:String,
    request_digest:String, invocation_digest:String, target_sha256:String, managed_tool_origin_v1:OrdinaryWriterOrigin,
}

impl WorkflowWriterContext {
    fn writer_metadata(&self)->Value {
        json!({"version":1,"session_id":self.session_id,"actor_id":self.actor_id,
            "execution_revision":self.execution_revision,"root_operation_id":self.root_operation_id,
            "inner_turn_id":self.inner_turn_id,"tool_call_id":self.tool_call_id,"source":self.source})
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct GraphCaptureContext {
    version:u8, account_id:String, actor_id:String, operation_id:String, action_id:String,
    request_sha256:String, target_sha256:String, scope_sha256:String, producer_sha256:String,
    native_receipt_sha256:String, result_sha256:String, result_length:u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct AuthorizedHistoryRoot {
    root_id: String,
    canonical_path: PathBuf,
    #[serde(default = "default_root_kind")]
    kind: String,
}

fn default_root_kind() -> String {
    "recursive_directory".into()
}

impl AuthorizedHistoryRoot {
    fn contains(&self, path: &Path) -> bool {
        if self.kind == "exact_file" {
            path == self.canonical_path
        } else {
            path.starts_with(&self.canonical_path)
        }
    }
}

impl HistoryServiceHook {
    #[cfg(target_os = "linux")]
    fn checkpoint_request(&self, payload: Value, deadline: std::time::Instant) -> Result<Value, String> {
        use std::io::Read;
        let remaining=deadline.checked_duration_since(std::time::Instant::now()).ok_or("checkpoint deadline elapsed")?;
        if remaining.is_zero() {return Err("checkpoint deadline elapsed".into());}
        let mut stream=UnixStream::connect(&self.socket_path).map_err(|_| "checkpoint unavailable")?;
        stream.set_read_timeout(Some(remaining)).map_err(|_| "checkpoint unavailable")?;
        stream.set_write_timeout(Some(remaining)).map_err(|_| "checkpoint unavailable")?;
        let mut frame=serde_json::to_vec(&payload).map_err(|_| "checkpoint unavailable")?;
        if frame.len()>1024*1024 {return Err("checkpoint frame too large".into());}
        frame.push(b'\n');let mut written=0usize;
        while written<frame.len() {
            let remaining=deadline.checked_duration_since(std::time::Instant::now()).ok_or("checkpoint deadline elapsed")?;
            if remaining.is_zero(){return Err("checkpoint deadline elapsed".into());}
            stream.set_write_timeout(Some(remaining)).map_err(|_| "checkpoint unavailable")?;
            let count=stream.write(&frame[written..]).map_err(|_| "checkpoint unavailable")?;
            if count==0{return Err("checkpoint unavailable".into());}written+=count;
        }
        let mut response=Vec::new();let mut buffer=[0u8;8192];
        loop {
            let remaining=deadline.checked_duration_since(std::time::Instant::now()).ok_or("checkpoint deadline elapsed")?;
            if remaining.is_zero(){return Err("checkpoint deadline elapsed".into());}
            stream.set_read_timeout(Some(remaining)).map_err(|_| "checkpoint unavailable")?;
            let count=stream.read(&mut buffer).map_err(|_| "checkpoint unavailable")?;
            if count==0 || response.len().saturating_add(count)>1024*1024 {return Err("checkpoint unavailable".into());}
            response.extend_from_slice(&buffer[..count]);
            if response.contains(&b'\n') {break;}
        }
        if response.last()!=Some(&b'\n') || response[..response.len()-1].contains(&b'\n') || std::time::Instant::now()>=deadline {return Err("checkpoint unavailable".into());}
        response.pop();let value:Value=serde_json::from_slice(&response).map_err(|_| "checkpoint unavailable")?;
        if value.get("Error").is_some() || value.get("Unavailable").is_some() {return Err("checkpoint unavailable".into());}
        Ok(value)
    }

    #[cfg(not(target_os = "linux"))]
    fn checkpoint_request(&self, _payload: Value, _deadline: std::time::Instant) -> Result<Value, String> {
        Err("checkpoint unavailable".into())
    }
    pub fn new(
        socket_path: impl Into<PathBuf>,
        actor_id: impl Into<String>,
        account_id: impl Into<String>,
        workspace_id: impl Into<String>,
        workspace_root: impl Into<PathBuf>,
        token: impl Into<String>,
    ) -> Result<Arc<Self>, String> {
        Self::new_with_actor_kind(
            socket_path,
            actor_id,
            account_id,
            workspace_id,
            workspace_root,
            token,
            "agent",
        )
    }

    pub fn new_with_actor_kind(
        socket_path: impl Into<PathBuf>,
        actor_id: impl Into<String>,
        account_id: impl Into<String>,
        workspace_id: impl Into<String>,
        workspace_root: impl Into<PathBuf>,
        token: impl Into<String>,
        actor_kind: impl Into<String>,
    ) -> Result<Arc<Self>, String> {
        let actor_id = actor_id.into();
        let account_id = account_id.into();
        let workspace_id = workspace_id.into();
        let workspace_root =
            fs::canonicalize(workspace_root.into()).map_err(|error| error.to_string())?;
        if actor_id.trim().is_empty()
            || account_id.trim().is_empty()
            || workspace_id.trim().is_empty()
        {
            return Err("history identity is incomplete".into());
        }
        let authorized_roots = match std::env::var("OPENCLANK_HISTORY_ROOT_BINDINGS") {
            Ok(raw) if !raw.trim().is_empty() => {
                let parsed = serde_json::from_str::<Vec<AuthorizedHistoryRoot>>(&raw)
                    .map_err(|error| format!("invalid history root bindings: {error}"))?;
                if parsed.is_empty() {
                    return Err("history root bindings are empty".into());
                }
                parsed
                    .into_iter()
                    .map(|mut root| {
                        root.canonical_path = fs::canonicalize(&root.canonical_path)
                            .map_err(|error| format!("history root is unavailable: {error}"))?;
                        if root.root_id.trim().is_empty() {
                            return Err("history root identity is empty".into());
                        }
                        if !matches!(root.kind.as_str(), "exact_file" | "recursive_directory") {
                            return Err("history root kind is invalid".into());
                        }
                        if root.kind == "exact_file" && !root.canonical_path.is_file() {
                            return Err("exact-file history root is not a file".into());
                        }
                        if root.kind == "recursive_directory" && !root.canonical_path.is_dir() {
                            return Err("recursive history root is not a directory".into());
                        }
                        Ok(root)
                    })
                    .collect::<Result<Vec<_>, String>>()?
            }
            _ => vec![AuthorizedHistoryRoot {
                root_id: "workspace-root".into(),
                canonical_path: workspace_root.clone(),
                kind: "recursive_directory".into(),
            }],
        };
        let checkpoint_tokens = match std::env::var("OPENCLANK_HISTORY_CHECKPOINT_TOKENS") {
            Ok(raw) if !raw.is_empty() => {
                let tokens: HashMap<String, String> = serde_json::from_str(&raw).map_err(|_| "invalid checkpoint bindings")?;
                if tokens.len() > authorized_roots.len() || tokens.iter().any(|(root, token)|
                    !authorized_roots.iter().any(|known| &known.root_id == root) || token.is_empty() || token.len() > 512) {
                    return Err("invalid checkpoint bindings".into());
                }
                tokens
            }
            _ => HashMap::new(),
        };
        let worktree_tokens = match std::env::var("OPENCLANK_HISTORY_WORKTREE_TOKENS") {
            Ok(raw) if !raw.is_empty() => {
                let tokens:HashMap<String,String>=serde_json::from_str(&raw).map_err(|_|"invalid worktree bindings")?;
                if tokens.len()>authorized_roots.len() || tokens.iter().any(|(root,token)|!authorized_roots.iter().any(|known|&known.root_id==root) || token.is_empty() || token.len()>512) {return Err("invalid worktree bindings".into());}
                tokens
            }, _=>HashMap::new(),
        };
        let hook = Arc::new(Self {
            mark_recovery: true,
            socket_path: socket_path.into(),
            actor_id,
            account_id,
            workspace_id,
            authorized_roots,
            token: token.into(),
            checkpoint_tokens,
            worktree_tokens,
            actor_kind: actor_kind.into(),
            workflow_writer: OnceLock::new(),
            ordinary_writer: OnceLock::new(),
            graph_capture: OnceLock::new(),
            statuses: Arc::new(Mutex::new(HashMap::new())),
            sequence: Arc::new(Mutex::new(0)),
            last_action: Arc::new(Mutex::new(None)),
        });
        hook.reconcile_registry_journal();
        Ok(hook)
    }

    /// Install one Host-verified private process context before serving requests.
    pub fn set_workflow_writer_context(&self,raw:&str,expected_agent:&str)->Result<(),String> {
        if raw.len()>8192 || self.actor_kind!="agent" || self.actor_id!=expected_agent {return Err("workflow writer unavailable".into());}
        let context:WorkflowWriterContext=serde_json::from_str(raw).map_err(|_|"workflow writer unavailable")?;
        let bounded=|value:&str| !value.is_empty() && value.len()<=256 && !value.chars().any(char::is_control);
        let hex=|value:&str| value.len()==64 && value.bytes().all(|c|c.is_ascii_digit() || (b'a'..=b'f').contains(&c));
        if context.version!=1 || context.account_id!=self.account_id
            || ![&context.account_id,&context.session_id,&context.actor_id,&context.root_operation_id,&context.inner_turn_id,&context.tool_call_id,
                &context.source.session_id,&context.source.actor_id,&context.source.root_operation_id,&context.source.inner_turn_id,&context.operation_id]
                .into_iter().all(|value|bounded(value))
            || context.execution_revision>9_007_199_254_740_991 || context.source.execution_revision>9_007_199_254_740_991
            || context.root_operation_id!=context.source.root_operation_id || !hex(&context.request_digest) || !hex(&context.target_sha256) {
            return Err("workflow writer unavailable".into());
        }
        self.workflow_writer.set(context).map_err(|_|"workflow writer unavailable".to_owned())
    }

    /// Host-created one-invocation ordinary writer attribution; scope remains independent authority.
    pub fn set_ordinary_writer_context(&self,raw:&str,expected_agent:&str)->Result<(),String> {
        if raw.len()>8192 || self.actor_kind!="agent" || self.actor_id!=expected_agent ||
            self.workflow_writer.get().is_some() || self.graph_capture.get().is_some() {return Err("ordinary writer unavailable".into());}
        let c:OrdinaryWriterContext=serde_json::from_str(raw).map_err(|_|"ordinary writer unavailable")?;
        let bounded=|v:&str| !v.is_empty() && v.len()<=256 && !v.chars().any(char::is_control);
        let hex=|v:&str| v.len()==64 && v.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        let o=&c.managed_tool_origin_v1;
        if c.version!=1 || c.account_id!=self.account_id || !matches!(c.canonical_tool.as_str(),"write_file"|"edit_file") ||
            !bounded(&c.operation_id) || ![&c.request_digest,&c.invocation_digest,&c.target_sha256].into_iter().all(|v|hex(v)) ||
            o.version!=1 || o.execution_revision>9_007_199_254_740_991 ||
            ![&o.host_chat_id,&o.engine_session_id,&o.root_operation_id,&o.actor_id,&o.inner_turn_id,&o.tool_call_id,&o.assistant_message_id].into_iter().all(|v|bounded(v)) {
            return Err("ordinary writer unavailable".into());
        }
        self.ordinary_writer.set(c).map_err(|_|"ordinary writer unavailable".to_owned())
    }

    /// Host-created one-invocation graph provenance; never a Files request field.
    pub fn set_graph_capture_context(&self, raw:&str)->Result<(),String> {
        if raw.len()>8192 || self.actor_kind!="human" || self.workflow_writer.get().is_some() {return Err("graph capture unavailable".into());}
        let c:GraphCaptureContext=serde_json::from_str(raw).map_err(|_|"graph capture unavailable")?;
        let id=|v:&str| !v.is_empty() && v.len()<=128 && v.bytes().all(|b|b.is_ascii_alphanumeric() || b==b'-' || b==b'_');
        let hex=|v:&str| v.len()==64 && v.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
        if c.version!=1 || c.account_id!=self.account_id || c.actor_id!=self.actor_id || !id(&c.operation_id) || !id(&c.action_id)
            || ![&c.request_sha256,&c.target_sha256,&c.scope_sha256,&c.producer_sha256,&c.native_receipt_sha256,&c.result_sha256].into_iter().all(|v|hex(v)) {return Err("graph capture unavailable".into());}
        let body=json!(["openclank.graph-topology.capture.v1",c.account_id,c.actor_id,c.operation_id,c.action_id,c.target_sha256,c.scope_sha256,c.producer_sha256,c.native_receipt_sha256,c.result_sha256,c.result_length]);
        if encode_hex(&Sha256::digest(serde_json::to_vec(&body).map_err(|_|"graph capture unavailable")?))!=c.request_sha256 {return Err("graph capture unavailable".into());}
        self.graph_capture.set(c).map_err(|_|"graph capture unavailable".to_owned())
    }

    fn validate_workflow_writer(&self,path:&Path,operation_id:&str,digest:&str)->Result<(),String> {
        if let Some(context)=self.workflow_writer.get() {
            let target=path.to_str().filter(|value|path.is_absolute() && !value.is_empty()).ok_or("workflow writer unavailable")?;
            if context.account_id!=self.account_id || context.operation_id!=operation_id || context.request_digest!=digest
                || context.target_sha256!=encode_hex(&Sha256::digest(target.as_bytes())) || !self.allowed(path) {
                return Err("workflow writer unavailable".into());
            }
        }
        Ok(())
    }

    fn next_action_id(&self) -> String {
        let mut sequence = self.sequence.lock().expect("history sequence lock");
        *sequence = sequence.saturating_add(1);
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        let action_id = format!("odysseus-{millis:x}-{:x}", *sequence);
        if let Ok(mut last) = self.last_action.lock() {
            *last = Some(action_id.clone());
        }
        action_id
    }

    fn set_status(&self, action_id: &str, status: &str, phase: &str, error: Option<String>) {
        if let Ok(mut statuses) = self.statuses.lock() {
            statuses.insert(
                action_id.to_owned(),
                CaptureStatus {
                    action_id: action_id.to_owned(),
                    status: status.to_owned(),
                    phase: phase.to_owned(),
                    error,
                },
            );
        }
    }

    fn allowed(&self, path: &Path) -> bool {
        let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        if self
            .authorized_roots
            .iter()
            .all(|root| !root.contains(&path))
        {
            return false;
        }
        path.components().all(|component| {
            let value = component.as_os_str().to_string_lossy().to_ascii_lowercase();
            !matches!(
                value.as_str(),
                ".env" | ".secrets" | "auth" | "credentials" | "tokens" | "settings"
            )
        })
    }

    #[cfg(target_os = "linux")]
    fn managed_root_for(&self, path: &Path) -> Result<(&AuthorizedHistoryRoot, PathBuf), String> {
        use std::ffi::CString;
        use std::os::fd::{AsRawFd, FromRawFd};
        use std::os::unix::ffi::OsStrExt;
        use std::os::unix::fs::OpenOptionsExt;
        use std::path::Component;
        if let Ok(found) = self.root_for(path) { return Ok(found); }
        if !path.is_absolute() || path.components().any(|part| matches!(part, Component::ParentDir | Component::CurDir)) {
            return Err("managed media path is invalid".into());
        }
        let root = self.authorized_roots.iter().filter(|root| root.kind == "recursive_directory" && path.starts_with(&root.canonical_path))
            .max_by_key(|root| root.canonical_path.components().count()).ok_or("managed media root is unavailable")?;
        let relative = path.strip_prefix(&root.canonical_path).map_err(|_| "managed media path is outside its root")?;
        let parts: Vec<_> = relative.components().collect();
        let v2 = parts.iter().any(|part| matches!(part, Component::Normal(name) if *name == ".media"));
        let v1 = parts.len() >= 3 && matches!(parts[parts.len() - 3], Component::Normal(name) if name == "media");
        if parts.len() < 3 || !(v1 || v2) {
            return Err("managed pending destination is not workspace media".into());
        }
        let mut options = fs::OpenOptions::new();
        options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
        let mut pinned = options.open(&root.canonical_path).map_err(|error| error.to_string())?;
        for part in &parts[..parts.len() - 1] {
            let Component::Normal(name) = part else { return Err("managed media path is invalid".into()); };
            let name = CString::new(name.as_bytes()).map_err(|_| "managed media path is invalid")?;
            // SAFETY: the descriptor and NUL-terminated component remain live.
            let fd = unsafe { libc::openat(pinned.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if fd < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::NotFound { return Ok((root, path.to_path_buf())); }
                return Err(format!("managed media ancestor is unavailable: {error}"));
            }
            // SAFETY: openat returned a fresh owned descriptor.
            pinned = unsafe { fs::File::from_raw_fd(fd) };
        }
        Err("managed media destination is unavailable".into())
    }

    #[cfg(not(target_os = "linux"))]
    fn managed_root_for(&self, _path: &Path) -> Result<(&AuthorizedHistoryRoot, PathBuf), String> {
        Err("managed pending media is unavailable on this platform".into())
    }

    fn root_for(&self, path: &Path) -> Result<(&AuthorizedHistoryRoot, PathBuf), String> {
        let canonical = self.canonical_target(path)?;
        self.authorized_roots
            .iter()
            .enumerate()
            .filter(|(_, root)| root.contains(&canonical))
            .max_by_key(|(index, root)| (root.canonical_path.components().count(), std::cmp::Reverse(*index)))
            .map(|(_, root)| (root, canonical))
            .ok_or_else(|| "resource is outside the authorized Files roots".into())
    }

    fn request(&self, payload: Value) -> Result<Value, String> {
        #[cfg(unix)]
        {
            let mut stream =
                UnixStream::connect(&self.socket_path).map_err(|error| error.to_string())?;
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(10)))
                .map_err(|error| error.to_string())?;
            let mut frame = serde_json::to_vec(&payload).map_err(|error| error.to_string())?;
            frame.push(b'\n');
            if frame.len() > 1024 * 1024 {
                return Err("history frame exceeds the 1 MiB service limit".into());
            }
            stream
                .write_all(&frame)
                .map_err(|error| error.to_string())?;
            // The history service keeps accepted connections alive for the
            // next request. Read one bounded newline-delimited response;
            // waiting for EOF would turn every real request into a timeout.
            let mut reader = BufReader::new(stream);
            let mut response = Vec::new();
            let read = reader
                .read_until(b'\n', &mut response)
                .map_err(|error| error.to_string())?;
            if read == 0 || response.last() != Some(&b'\n') {
                return Err("history service returned an unterminated response frame".into());
            }
            response.pop();
            if response.len() > 1024 * 1024 {
                return Err("history response exceeds the 1 MiB service limit".into());
            }
            let value: Value =
                serde_json::from_slice(&response).map_err(|error| error.to_string())?;
            if value.get("Error").is_some() || value.get("error").is_some() {
                return Err(value.to_string());
            }
            Ok(value)
        }
        #[cfg(windows)]
        {
            let mut frame = serde_json::to_vec(&payload).map_err(|error| error.to_string())?;
            frame.push(b'\n');
            if frame.len() > 1024 * 1024 { return Err("history frame exceeds the 1 MiB service limit".into()); }
            let response = windows_channel::exchange(&self.socket_path, &frame).map_err(|error| error.to_string())?;
            let value: Value = serde_json::from_slice(&response).map_err(|error| error.to_string())?;
            if value.get("Error").is_some() || value.get("error").is_some() { return Err(value.to_string()); }
            Ok(value)
        }
    }

    fn auth(&self) -> Value {
        json!({"actor_id": self.actor_id, "account_id": self.account_id, "token": self.token})
    }

    fn fingerprint(bytes: Option<&[u8]>) -> String {
        match bytes {
            None => "missing".into(),
            Some(bytes) => format!(
                "sha256:{}:{}",
                encode_hex(&Sha256::digest(bytes)),
                bytes.len()
            ),
        }
    }

    /// Transfer the journal-pinned bytes with bounded frames. An error may
    /// represent an issued request; return its original upload ID for status
    /// reconciliation and never silently abort or issue a replacement.
    #[cfg(target_os="linux")]
    pub(crate) fn stage_worktree_history(&self, stage:&crate::engine::WorktreeHistoryStage<'_>, deadline:std::time::Instant) -> Result<String,(String,String)> {
        use std::os::unix::fs::FileExt;
        let mut upload=String::new();
        let run=(|| -> Result<(),String> {
            if stage.actor_id()!=self.actor_id || stage.account_id()!=self.account_id {return Err("worktree stage account or actor mismatch".into());}
            let token=self.worktree_tokens.get(stage.root_id()).ok_or("worktree role unavailable")?;
            stage.verify_until(deadline).map_err(|_|"worktree stage changed")?;
            let role=json!({"protocol_version":1,"auth":{"actor_id":self.actor_id,"account_id":self.account_id,"token":token},"action_id":stage.action_id()});
            let claimed=self.checkpoint_request(json!({"ClaimFilesWorktree":{"envelope":role,"root_id":stage.root_id()}}),deadline)?;
            let slot=claimed.get("FilesWorktreeSlot").ok_or("worktree claim unconfirmed")?;
            upload=slot.get("upload_id").and_then(Value::as_str).filter(|s|s.starts_with("odysseus-worktree-")).ok_or("worktree slot unavailable")?.to_owned();
            if slot.get("fingerprint").and_then(Value::as_str)!=Some(stage.fingerprint()) || slot.get("content_length").and_then(Value::as_u64)!=Some(stage.length()) {return Err("worktree slot changed".into());}
            let control=json!({"protocol_version":1,"auth":self.auth(),"action_id":stage.action_id()});
            if self.checkpoint_request(json!({"StageBegin":{"envelope":control,"upload_id":upload,"content_length":stage.length(),"fingerprint":stage.fingerprint()}}),deadline)? != json!("Accepted") {return Err("worktree stage admission unconfirmed".into());}
            let mut offset=0u64;let mut buffer=vec![0u8;128*1024];
            while offset<stage.length() {
                let want=((stage.length()-offset).min(buffer.len() as u64)) as usize;
                let n=stage.file().read_at(&mut buffer[..want],offset).map_err(|_|"worktree stage unavailable")?;
                if n==0 {return Err("worktree stage truncated".into());}
                if self.checkpoint_request(json!({"StageChunk":{"envelope":control,"upload_id":upload,"offset":offset,"content":encode_base64(&buffer[..n])}}),deadline)? != json!("Accepted") {return Err("worktree stage chunk unconfirmed".into());}
                offset+=n as u64;
            }
            stage.verify_until(deadline).map_err(|_|"worktree stage changed")?;
            let finished=self.checkpoint_request(json!({"StageFinish":{"envelope":control,"upload_id":upload}}),deadline)?;
            if finished.get("Staged") != Some(&json!({"upload_id":upload,"content_length":stage.length()})) {return Err("worktree stage completion unconfirmed".into());}
            stage.verify_until(deadline).map_err(|_|"worktree stage changed")?;
            let result=self.checkpoint_request(json!({"CompleteFilesWorktree":{"envelope":role,"root_id":stage.root_id(),"upload_id":upload}}),deadline)?;
            let action=result.get("Action").ok_or("worktree receipt unconfirmed")?;
            if action.get("action_id").and_then(Value::as_str)!=Some(stage.action_id()) || action.get("state").and_then(Value::as_str)!=Some("AfterDurable") {return Err("worktree receipt unconfirmed".into());}
            stage.verify_until(deadline).map_err(|_|"worktree stage changed")?;
            let finalized=self.checkpoint_request(json!({"FinalizeFilesWorktree":{"envelope":role,"root_id":stage.root_id()}}),deadline)?;
            let action=finalized.get("Action").ok_or("worktree finalization unconfirmed")?;
            if action.get("action_id").and_then(Value::as_str)!=Some(stage.action_id()) || action.get("state").and_then(Value::as_str)!=Some("Complete") {return Err("worktree finalization unconfirmed".into());}
            Ok(())
        })();
        run.map(|()|upload.clone()).map_err(|error|(upload,error))
    }

    #[cfg(test)]
    pub(crate) fn set_worktree_test_token(this:&mut Arc<Self>, root:&str, token:&str) {Arc::get_mut(this).unwrap().worktree_tokens.insert(root.into(),token.into());}

    fn stage_content(
        &self,
        action_id: &str,
        content: &[u8],
        fingerprint: &str,
    ) -> Result<String, String> {
        if action_id.trim().is_empty() {
            return Err("history staged capture requires an action id".into());
        }
        let mut random = [0_u8; 24];
        getrandom::fill(&mut random).map_err(|error| error.to_string())?;
        let upload_id = format!("odysseus-stage-{}", encode_hex(&random));
        self.stage_content_with_id(action_id, content, fingerprint, upload_id, false)
    }

    fn stage_content_with_id(
        &self, action_id: &str, content: &[u8], fingerprint: &str,
        upload_id: String, recoverable: bool,
    ) -> Result<String, String> {
        if recoverable {
            // The coordinator has already checked remote action state and
            // holds the operation lock. Discard only this exact stage ID.
            let _ = self.request(json!({"StageAbort": {"envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id}, "upload_id": upload_id}}));
        }
        let result = (|| {
            self.request(json!({
                "StageBegin": {
                    "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                    "upload_id": upload_id,
                    "content_length": content.len(),
                    "fingerprint": fingerprint,
                }
            }))?;
            for (offset, chunk) in content.chunks(STAGE_CHUNK_BYTES).enumerate() {
                let offset = offset * STAGE_CHUNK_BYTES;
                self.request(json!({
                    "StageChunk": {
                        "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                        "upload_id": upload_id,
                        "offset": offset,
                        "content": encode_base64(chunk),
                    }
                }))?;
            }
            self.request(json!({
                "StageFinish": {
                    "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                    "upload_id": upload_id,
                }
            }))?;
            Ok(upload_id.clone())
        })();
        if result.is_err() && !recoverable {
            let _ = self.request(json!({
                "StageAbort": {
                    "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                    "upload_id": upload_id,
                }
            }));
        }
        result
    }

    fn canonical_target(&self, path: &Path) -> Result<PathBuf, String> {
        if let Ok(existing) = fs::canonicalize(path) {
            return Ok(existing);
        }
        let parent = path
            .parent()
            .ok_or_else(|| "resource has no parent".to_owned())?;
        let parent = fs::canonicalize(parent).map_err(|error| error.to_string())?;
        let name = path
            .file_name()
            .ok_or_else(|| "resource has no filename".to_owned())?;
        Ok(parent.join(name))
    }

    /// Ask the history worker to own the path-to-id binding.  The provider
    /// sends only its already-authorized root and a relative path; the worker
    /// validates both and returns an opaque handle without returning the
    /// private absolute path map.
    fn register_resource(&self, path: &Path) -> Result<(Value, bool), String> {
        self.register_resource_inner(path, false)
    }

    fn register_resource_inner(&self, path: &Path, managed_pending: bool) -> Result<(Value, bool), String> {
        self.register_resource_inner_selected(path, managed_pending, None)
    }

    fn register_resource_inner_selected(&self, path: &Path, managed_pending: bool, selected_root: Option<&str>) -> Result<(Value, bool), String> {
        let (root, canonical) = if managed_pending { self.managed_root_for(path)? } else { self.root_for(path)? };
        if selected_root.is_some_and(|id| root.root_id != id) {return Err("selected registration root changed".into());}
        let relative_path = canonical
            .strip_prefix(&root.canonical_path)
            .map_err(|_| "resource is outside the authorized Files root".to_owned())?
            .to_string_lossy()
            .replace('\\', "/");
        let relative_path = if relative_path.is_empty() {
            ".".to_owned()
        } else {
            relative_path
        };
        let body = json!({
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "account_id": self.account_id,
                "workspace_id": self.workspace_id,
                "root_id": root.root_id,
                "root_path": root.canonical_path,
                "relative_path": relative_path,
                "resource_id": null
        });
        let request = if managed_pending { json!({"RegisterManagedMediaDestination":body}) } else { json!({"RegisterResource":body}) };
        let response = self.request(request)?;
        let resource = response
            .get("Resource")
            .and_then(|value| value.get("handle"))
            .ok_or_else(|| "history service returned no resource handle".to_owned())?;
        let resource_id = resource
            .get("resource_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| "history service returned an invalid resource handle".to_owned())?
            .to_owned();
        let created = response
            .get("Resource")
            .and_then(|value| value.get("created"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let generation = resource.get("generation").and_then(Value::as_u64)
            .ok_or_else(|| "history service returned an invalid resource generation".to_owned())?;
        Ok((json!({"resource_id":resource_id, "generation":generation, "root_id":root.root_id,
            "root_path":root.canonical_path, "relative_path":relative_path}), created))
    }

    fn update_resource(&self, resource_id: &str, path: &Path) -> Result<(), String> {
        let (root, canonical) = self.root_for(path)?;
        let relative_path = canonical
            .strip_prefix(&root.canonical_path)
            .map_err(|_| "resource is outside the authorized Files root".to_owned())?
            .to_string_lossy()
            .replace('\\', "/");
        let relative_path = if relative_path.is_empty() {
            ".".to_owned()
        } else {
            relative_path
        };
        self.request(json!({
            "UpdateResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "resource_id": resource_id,
                "account_id": self.account_id,
                "workspace_id": self.workspace_id,
                "root_id": root.root_id,
                "root_path": root.canonical_path,
                "relative_path": relative_path
            }
        }))?;
        Ok(())
    }

    fn move_resource(
        &self,
        resource_id: &str,
        replaced_resource_id: Option<&str>,
        path: &Path,
    ) -> Result<(), String> {
        let (root, canonical) = self.root_for(path)?;
        let relative_path = canonical
            .strip_prefix(&root.canonical_path)
            .map_err(|_| "resource is outside the authorized Files root".to_owned())?
            .to_string_lossy()
            .replace('\\', "/");
        let relative_path = if relative_path.is_empty() {
            ".".to_owned()
        } else {
            relative_path
        };
        self.request(json!({
            "MoveResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "resource_id": resource_id,
                "replaced_resource_id": replaced_resource_id,
                "account_id": self.account_id,
                "workspace_id": self.workspace_id,
                "root_id": root.root_id,
                "root_path": root.canonical_path,
                "relative_path": relative_path
            }
        }))?;
        Ok(())
    }

    fn revoke_resource(&self, resource_id: &str, reason: &str) -> Result<(), String> {
        self.request(json!({
            "RevokeResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "resource_id": resource_id,
                "reason": reason
            }
        }))?;
        Ok(())
    }

    fn registry_journal_dir(&self) -> Option<PathBuf> {
        self.authorized_roots
            .first()
            .map(|root| root.canonical_path.join(".openclank-registry-journal"))
    }

    fn write_registry_journal(&self, action_id: &str, commit: &RegistryCommit) -> Result<PathBuf, String> {
        let directory = self
            .registry_journal_dir()
            .ok_or_else(|| "no authorized root for registry journal".to_owned())?;
        fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
        let path = directory.join(format!("{action_id}.json"));
        let temporary = directory.join(format!(".{action_id}.tmp-{}", std::process::id()));
        let payload = serde_json::to_vec(&RegistryJournal {
            action_id: action_id.to_owned(),
            commit: commit.clone(),
        })
        .map_err(|error| error.to_string())?;
        fs::write(&temporary, payload).map_err(|error| error.to_string())?;
        fs::rename(&temporary, &path).map_err(|error| error.to_string())?;
        if let Ok(directory) = fs::File::open(&directory) {
            let _ = directory.sync_all();
        }
        Ok(path)
    }

    fn apply_registry_commit(&self, commit: &RegistryCommit) -> Result<(), String> {
        match commit {
            RegistryCommit::Move {
                source_id,
                destination_id,
                destination,
            } => self.move_resource(
                source_id,
                (destination_id != source_id).then_some(destination_id.as_str()),
                destination,
            ),
            RegistryCommit::Revoke { resource_id } => {
                self.revoke_resource(resource_id, "resource deleted")
            }
        }
    }

    fn reconcile_registry_journal(&self) {
        let Some(directory) = self.registry_journal_dir() else { return };
        let Ok(entries) = fs::read_dir(directory) else { return };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) != Some("json") {
                continue;
            }
            let Ok(payload) = fs::read(&path) else { continue };
            let Ok(journal) = serde_json::from_slice::<RegistryJournal>(&payload) else { continue };
            if self.apply_registry_commit(&journal.commit).is_ok() {
                let _ = fs::remove_file(path);
            }
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
enum RegistryCommit {
    Move {
        source_id: String,
        destination_id: String,
        destination: PathBuf,
    },
    Revoke {
        resource_id: String,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct RegistryJournal {
    action_id: String,
    commit: RegistryCommit,
}

struct HistoryCheckpointTicket {
    hook: HistoryServiceHook,
    request_id: String,
    root_id: String,
    action_id: String,
    selections: Vec<CheckpointSelection>,
    bindings: Vec<CheckpointBinding>,
    staged: Vec<Value>,
    staged_possible: bool,
}

impl HistoryCheckpointTicket {
    fn role_auth(&self) -> Result<Value, String> {
        let token=self.hook.checkpoint_tokens.get(&self.root_id).ok_or("checkpoint unavailable")?;
        Ok(json!({"actor_id":self.hook.actor_id,"account_id":self.hook.account_id,"token":token}))
    }
    fn envelope(&self) -> Result<Value, String> {
        Ok(json!({"protocol_version":1,"auth":self.role_auth()?,"action_id":self.action_id}))
    }
}

impl CheckpointTicket for HistoryCheckpointTicket {
    fn bindings(&self) -> &[CheckpointBinding] { &self.bindings }
    fn stage(&mut self, index: usize, bytes: &[u8], deadline: std::time::Instant) -> Result<(), String> {
        if index != self.staged.len() || index >= self.selections.len() { return Err("checkpoint stage order invalid".into()); }
        let item=&self.selections[index];
        if Self::fingerprint_for(bytes) != item.expected_fingerprint { return Err("checkpoint bytes changed".into()); }
        let upload=self.bindings[index].staged_upload_id.clone();
        if !upload.starts_with("odysseus-checkpoint-") {return Err("checkpoint slot unavailable".into());}
        let control=json!({"protocol_version":1,"auth":self.hook.auth(),"action_id":self.action_id});
        self.staged_possible = true;
        let stage_result=(|| -> Result<(),String> {
            self.hook.checkpoint_request(json!({"StageBegin":{"envelope":control,"upload_id":upload,"content_length":bytes.len(),"fingerprint":item.expected_fingerprint}}),deadline)?;
            for (index,chunk) in bytes.chunks(STAGE_CHUNK_BYTES).enumerate() {
                self.hook.checkpoint_request(json!({"StageChunk":{"envelope":control,"upload_id":upload,"offset":index*STAGE_CHUNK_BYTES,"content":encode_base64(chunk)}}),deadline)?;
            }
            self.hook.checkpoint_request(json!({"StageFinish":{"envelope":control,"upload_id":upload}}),deadline)?;
            Ok(())
        })();
        if stage_result.is_err() {
            let cleanup=std::time::Instant::now()+std::time::Duration::from_secs(1);
            let _=self.hook.checkpoint_request(json!({"StageAbort":{"envelope":control,"upload_id":upload}}),cleanup);
        }
        stage_result?;
        self.staged.push(json!({"resource_id":item.resource_id,"staged_upload_id":upload,"fingerprint":item.expected_fingerprint,"size":bytes.len()}));
        Ok(())
    }
    fn finalize(self: Box<Self>, started_millis: u64, finished_millis: u64, deadline: std::time::Instant) -> Result<Value, String> {
        if self.staged.len()!=self.selections.len() { return Err("checkpoint stages incomplete".into()); }
        let response=self.hook.checkpoint_request(json!({"FinalizeSelectedCheckpoint": {"envelope":self.envelope()?,"request_id":self.request_id,
            "workspace_id":self.hook.workspace_id,"root_id":self.root_id,"items":self.selections,"staged":self.staged,
            "started_millis":started_millis,"finished_millis":finished_millis}}),deadline)?;
        let result=response.get("CheckpointStatus").ok_or("checkpoint outcome unavailable")?;
        if result.get("state").and_then(Value::as_str)!=Some("complete") {return Err("checkpoint outcome unavailable".into());}
        Ok(result.clone())
    }
    fn fail(self: Box<Self>) {
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(2);
        for stage in &self.staged {
            if let Some(id)=stage.get("staged_upload_id").and_then(Value::as_str) {
                let _=self.hook.checkpoint_request(json!({"StageAbort":{"envelope":{"protocol_version":1,"auth":self.hook.auth(),"action_id":self.action_id},"upload_id":id}}),deadline);
            }
        }
        // Generic StageAbort does not prove unlink completion. Once staging
        // was attempted, retain the original claim as unknown instead of
        // reporting a false terminal cleanup receipt.
        if !self.staged_possible {
            if let Ok(envelope)=self.envelope() {
                let _=self.hook.checkpoint_request(json!({"FailSelectedCheckpoint":{"envelope":envelope,"request_id":self.request_id,
                    "workspace_id":self.hook.workspace_id,"root_id":self.root_id,"items":self.selections}}),deadline);
            }
        }
    }
}

impl HistoryCheckpointTicket {
    fn fingerprint_for(bytes: &[u8]) -> String { format!("sha256:{}:{}",encode_hex(&Sha256::digest(bytes)),bytes.len()) }
}

impl MutationCaptureHook for HistoryServiceHook {
    fn ordinary_write_ready(&self) -> bool {
        !self.account_id.is_empty() && !self.actor_id.is_empty() && !self.token.is_empty()
            && !self.authorized_roots.is_empty() && self.socket_path.exists()
    }

    fn checkpoint_ready(&self) -> bool { cfg!(target_os="linux") && !self.checkpoint_tokens.is_empty() }
    fn checkpoint_root(&self, root_id: &str) -> Result<PathBuf, String> {
        if !cfg!(target_os="linux") || !self.checkpoint_tokens.contains_key(root_id) {return Err("selected registration unavailable".into());}
        self.authorized_roots.iter().find(|root|root.root_id==root_id && root.kind=="recursive_directory")
            .map(|root|root.canonical_path.clone()).ok_or_else(||"selected registration unavailable".into())
    }
    fn register_checkpoint_file(&self, path: &Path, root_id: &str) -> Result<Value, String> {
        if !cfg!(target_os="linux") || !self.checkpoint_tokens.contains_key(root_id) {return Err("selected registration unavailable".into());}
        let (identity, created)=self.register_resource_inner_selected(path, false, Some(root_id))?;
        Ok(json!({"identity":identity,"created":created}))
    }
    fn claim_checkpoint(&self, request_id: &str, root_id: &str, items: &[CheckpointSelection], deadline: std::time::Instant) -> Result<Box<dyn CheckpointTicket>, String> {
        let token=self.checkpoint_tokens.get(root_id).ok_or("checkpoint unavailable")?;
        let action_id=format!("checkpoint-{}",encode_hex(&Sha256::digest(format!("{}\0{}\0{}",self.account_id,self.actor_id,request_id).as_bytes())));
        let response=self.checkpoint_request(json!({"ClaimSelectedCheckpoint":{"envelope":{"protocol_version":1,"auth":{"actor_id":self.actor_id,
            "account_id":self.account_id,"token":token},"action_id":action_id},"request_id":request_id,"workspace_id":self.workspace_id,"root_id":root_id,"items":items}}),deadline)?;
        let claim=response.get("CheckpointClaim").ok_or("checkpoint already issued or unavailable")?;
        if claim.get("state").and_then(Value::as_str)!=Some("claimed") {return Err("checkpoint unavailable".into());}
        let mut bindings:Vec<CheckpointBinding>=serde_json::from_value(claim.get("bindings").cloned().ok_or("checkpoint binding unavailable")?).map_err(|_| "checkpoint binding unavailable")?;
        if bindings.len()!=items.len() || bindings.iter().zip(items).any(|(binding,item)|binding.resource_id!=item.resource_id || binding.root_id!=root_id || binding.workspace_id!=self.workspace_id) {return Err("checkpoint binding changed".into());}
        let root=self.authorized_roots.iter().find(|root|root.root_id==root_id && root.kind=="recursive_directory").ok_or("checkpoint root unavailable")?;
        for binding in &mut bindings { binding.canonical_root=root.canonical_path.clone(); }
        Ok(Box::new(HistoryCheckpointTicket {hook:self.clone(),request_id:request_id.into(),root_id:root_id.into(),action_id,
            selections:items.to_vec(),bindings,staged:Vec::new(),staged_possible:false}))
    }
    fn legacy_prepare_hook(&self) -> Option<Box<dyn MutationCaptureHook>> {
        let mut hook = self.clone();
        hook.mark_recovery = false;
        Some(Box::new(hook))
    }

    fn settle_recovery(&self, action_id: &str) -> Result<(), String> {
        let response = self.request(json!({"SettleRecovery": {
            "protocol_version":1, "auth":self.auth(), "action_id":action_id
        }}))?;
        let action = response.get("Action").ok_or("history settlement unavailable")?;
        if action["action_id"].as_str() != Some(action_id)
            || (action["recovery_only"] == true && action["recovery_settled"] != true) {
            return Err("history settlement unconfirmed".into());
        }
        Ok(())
    }

    fn managed_account_id(&self) -> Result<String, String> { Ok(self.account_id.clone()) }
    fn managed_actor_id(&self) -> Result<String, String> { Ok(self.actor_id.clone()) }
    #[cfg(target_os="linux")]
    fn finish_worktree_history(&self,stage:&crate::engine::WorktreeHistoryStage<'_>,live:&Value,deadline:std::time::Instant)->Result<(),String>{
        if stage.actor_id()!=self.actor_id || stage.account_id()!=self.account_id || live.get("action_id").and_then(Value::as_str)!=Some(stage.action_id()) || live.get("status").and_then(Value::as_str)!=Some("Committed") || live.get("fingerprint").and_then(Value::as_str)!=Some(stage.fingerprint()) || live.get("committed_resources").and_then(Value::as_array).map(Vec::len)!=Some(1) {return Err("worktree live receipt unavailable".into())}
        stage.verify_until(deadline).map_err(|_|"worktree stage changed")?;
        let envelope=json!({"protocol_version":1,"auth":self.auth(),"action_id":stage.action_id()});
        let reply=self.checkpoint_request(json!({"RecordLive":{"envelope":envelope,"receipt":live}}),deadline)?;
        let action=reply.get("Action").ok_or("worktree live outcome unavailable")?;
        if action.get("action_id").and_then(Value::as_str)!=Some(stage.action_id()) || action.get("state").and_then(Value::as_str)!=Some("Applied") {return Err("worktree live outcome unavailable".into())}
        self.stage_worktree_history(stage,deadline).map(|_|()).map_err(|_|"worktree History outcome unavailable".into())
    }
    fn managed_worktree_capture_ready(&self,root_id:&str,deadline:std::time::Instant)->Result<bool,String>{
        if self.actor_kind!="human" || !self.worktree_tokens.get(root_id).is_some_and(|v|!v.is_empty()) || !self.authorized_roots.iter().any(|v|v.root_id==root_id) {return Ok(false)}
        let envelope=json!({"protocol_version":1,"auth":self.auth(),"action_id":"worktree-capabilities"});
        let response=self.checkpoint_request(json!({"FilesWorktreeCapabilities":envelope}),deadline)?;
        Ok(response.pointer("/FilesWorktreeCapabilities/worktree_capture").and_then(Value::as_bool)==Some(true))
    }

    #[cfg(target_os="linux")]
    fn lookup_worktree_history_version(&self,root_id:&str,action_id:&str,operation_id:&str,deadline:std::time::Instant)->Result<String,String>{
        if self.actor_kind!="human" {return Err("worktree version unavailable".into())}
        let token=self.worktree_tokens.get(root_id).ok_or("worktree role unavailable")?;
        let envelope=json!({"protocol_version":1,"auth":{"actor_id":self.actor_id,"account_id":self.account_id,"token":token},"action_id":action_id});
        let response=self.checkpoint_request(json!({"LookupFilesWorktreeVersion":{"envelope":envelope,"root_id":root_id,"operation_id":operation_id}}),deadline)?;
        response.pointer("/FilesWorktreeVersion/version_id").and_then(Value::as_str).filter(|v|!v.is_empty() && v.len()<=256 && !v.bytes().any(|b|b.is_ascii_control())).map(str::to_owned).ok_or("worktree version unavailable".into())
    }

    #[cfg(target_os="linux")]
    fn prepare_worktree_redo_history(&self,stage:&crate::engine::WorktreeRedoReadStage<'_>,deadline:std::time::Instant)->Result<Value,String>{
        use std::os::unix::fs::FileExt;use base64::Engine as _;
        let b=stage.binding();if self.actor_kind!="human" || self.actor_id!=b.actor_id || self.account_id!=b.account_id
            || b.resource_key.get("account_id").and_then(Value::as_str)!=Some(self.account_id.as_str())
            || b.resource_key.get("workspace_id").and_then(Value::as_str)!=Some(self.workspace_id.as_str()) {return Err("worktree redo unavailable".into())}
        let token=self.worktree_tokens.get(&b.root_id).ok_or("worktree role unavailable")?;
        let tree=json!({"version":2,"allowed_after_format":"indexed_tree_v2","commit_oid":b.commit_oid,"tree_oid":b.tree_oid,"target_manifest_sha256":b.target_manifest_sha256,"journal_operation_id":b.operation_id});
        let redo=json!({"journal_operation_id":b.operation_id,"inverse_action_id":b.inverse_action_id,"inverse_version_id":b.inverse_version_id,"container_sha256":b.container_sha256,"container_length":b.container_length});
        let request=json!({"schema_version":1,"action_id":b.action_id,"actor_account_id":b.account_id,"resource_key":b.resource_key,"guard_resource_ids":[],"modified_resource_ids":[b.resource_key],
            "operation":"source_worktree_redo_v2","actor_id":b.actor_id,"actor_kind":"human","tool_id":"odysseus-files","timestamp_millis":b.timestamp_millis,
            "coverage":{"metadata":{"required_recovery":true,"source_worktree_tree_v2":tree,"source_worktree_redo_v2":redo}},"per_resource_outcomes":null});
        let auth=json!({"actor_id":self.actor_id,"account_id":self.account_id,"token":token});
        let envelope=json!({"protocol_version":1,"auth":auth,"claimed_digest":"","request":request});
        let response=self.checkpoint_request(json!({"PrepareFilesWorktreeRedo":{"envelope":envelope,"root_id":b.root_id,"inverse_action_id":b.inverse_action_id,"inverse_version_id":b.inverse_version_id}}),deadline)?;
        let prepared=response.get("FilesWorktreeRedoPrepared").ok_or("worktree redo unavailable")?;
        let action=prepared.get("action").cloned().ok_or("worktree redo unavailable")?;
        let fingerprint=format!("sha256:{}:{}",b.container_sha256,b.container_length);
        let id=prepared.get("read_id").and_then(Value::as_str).filter(|v|v.starts_with("odysseus-worktree-read-")&&v.len()<=128).ok_or("worktree redo unavailable")?;
        if action.get("action_id").and_then(Value::as_str)!=Some(b.action_id.as_str()) || action.get("state").and_then(Value::as_str)!=Some("BeforeDurable")
            || action.get("resource_key")!=Some(&b.resource_key) || prepared.get("content_length").and_then(Value::as_u64)!=Some(b.container_length)
            || prepared.get("fingerprint").and_then(Value::as_str)!=Some(fingerprint.as_str()) {return Err("worktree redo changed".into())}
        let control=json!({"protocol_version":1,"auth":auth,"action_id":b.action_id});let mut offset=0u64;
        while offset<b.container_length {
            let response=self.checkpoint_request(json!({"FilesWorktreeReadChunk":{"envelope":control,"root_id":b.root_id,"read_id":id,"offset":offset}}),deadline)?;
            let chunk=response.get("Chunk").ok_or("worktree redo unavailable")?;if chunk.get("offset").and_then(Value::as_u64)!=Some(offset){return Err("worktree redo changed".into())}
            let encoded=chunk.get("content").and_then(Value::as_str).filter(|v|v.len()<=174764).ok_or("worktree redo unavailable")?;
            let bytes=base64::engine::general_purpose::STANDARD.decode(encoded).map_err(|_|"worktree redo unavailable")?;
            if bytes.is_empty() || bytes.len()>128*1024 || base64::engine::general_purpose::STANDARD.encode(&bytes)!=encoded {return Err("worktree redo unavailable".into())}
            let next=offset.checked_add(bytes.len() as u64).filter(|v|*v<=b.container_length).ok_or("worktree redo unavailable")?;
            if chunk.get("eof").and_then(Value::as_bool)!=Some(next==b.container_length){return Err("worktree redo unavailable".into())}
            stage.file().write_all_at(&bytes,offset).map_err(|_|"worktree redo unavailable")?;offset=next;
        }
        Ok(action)
    }

    #[cfg(target_os="linux")]
    fn prepare_worktree_inverse_history(&self,binding:&crate::engine::WorktreeInverseHistoryBinding,deadline:std::time::Instant)->Result<Value,String>{
        if self.actor_kind!="human" || self.actor_id!=binding.actor_id || self.account_id!=binding.account_id
            || binding.resource_key.get("account_id").and_then(Value::as_str)!=Some(self.account_id.as_str())
            || binding.resource_key.get("workspace_id").and_then(Value::as_str)!=Some(self.workspace_id.as_str()) {return Err("worktree inverse unavailable".into())}
        let token=self.worktree_tokens.get(&binding.root_id).ok_or("worktree role unavailable")?;
        let provenance=json!({"journal_operation_id":binding.operation_id,"original_action_id":binding.original_action_id,"original_version_id":binding.original_version_id,
            "original_journal_operation_id":binding.original_operation_id,"original_container_sha256":binding.container_sha256,"original_container_length":binding.container_length});
        let request=json!({"schema_version":1,"action_id":binding.action_id,"actor_account_id":binding.account_id,
            "resource_key":binding.resource_key,"guard_resource_ids":[],"modified_resource_ids":[binding.resource_key],
            "operation":"source_worktree_inverse_v2","actor_id":binding.actor_id,"actor_kind":"human","tool_id":"odysseus-files",
            "timestamp_millis":binding.timestamp_millis,"coverage":{"metadata":{"required_recovery":true,"source_worktree_tree_v2":provenance}},"per_resource_outcomes":null});
        let envelope=json!({"protocol_version":1,"auth":{"actor_id":self.actor_id,"account_id":self.account_id,"token":token},"claimed_digest":"","request":request});
        let result=self.checkpoint_request(json!({"PrepareFilesWorktreeInverse":{"envelope":envelope,"root_id":binding.root_id,
            "original_action_id":binding.original_action_id,"original_version_id":binding.original_version_id}}),deadline)?;
        let action=result.get("Action").cloned().ok_or("worktree inverse outcome unavailable")?;
        if action.get("action_id").and_then(Value::as_str)!=Some(binding.action_id.as_str()) || action.get("state").and_then(Value::as_str)!=Some("BeforeDurable") {return Err("worktree inverse outcome unavailable".into())}Ok(action)
    }

    #[cfg(target_os="linux")]
    fn read_worktree_inverse_history(&self,stage:&crate::engine::WorktreeInverseReadStage<'_>,deadline:std::time::Instant)->Result<(),String>{
        use std::os::unix::fs::FileExt;use base64::Engine as _;
        if self.actor_kind!="human" || self.actor_id!=stage.actor_id() || self.account_id!=stage.account_id(){return Err("worktree inverse read unavailable".into())}
        let token=self.worktree_tokens.get(stage.root_id()).ok_or("worktree role unavailable")?;
        let envelope=json!({"protocol_version":1,"auth":{"actor_id":self.actor_id,"account_id":self.account_id,"token":token},"action_id":stage.action_id()});
        let response=self.checkpoint_request(json!({"BeginFilesWorktreeRead":{"envelope":envelope,"root_id":stage.root_id(),"version_id":stage.version_id()}}),deadline)?;
        let read=response.get("FilesWorktreeRead").ok_or("worktree read unavailable")?;
        let id=read.get("read_id").and_then(Value::as_str).filter(|v|v.starts_with("odysseus-worktree-read-")&&v.len()<=128).ok_or("worktree read unavailable")?;
        if read.get("content_length").and_then(Value::as_u64)!=Some(stage.length()) || read.get("fingerprint").and_then(Value::as_str)!=Some(stage.fingerprint()){return Err("worktree read changed".into())}
        let mut offset=0u64;while offset<stage.length(){
            let response=self.checkpoint_request(json!({"FilesWorktreeReadChunk":{"envelope":envelope,"root_id":stage.root_id(),"read_id":id,"offset":offset}}),deadline)?;
            let chunk=response.get("Chunk").ok_or("worktree chunk unavailable")?;if chunk.get("offset").and_then(Value::as_u64)!=Some(offset){return Err("worktree chunk changed".into())}
            let encoded=chunk.get("content").and_then(Value::as_str).filter(|v|v.len()<=174764).ok_or("worktree chunk unavailable")?;
            let bytes=base64::engine::general_purpose::STANDARD.decode(encoded).map_err(|_|"worktree chunk invalid")?;
            if bytes.is_empty() || bytes.len()>128*1024 || base64::engine::general_purpose::STANDARD.encode(&bytes)!=encoded{return Err("worktree chunk invalid".into())}
            let next=offset.checked_add(bytes.len() as u64).filter(|v|*v<=stage.length()).ok_or("worktree chunk invalid")?;
            if chunk.get("eof").and_then(Value::as_bool)!=Some(next==stage.length()){return Err("worktree chunk invalid".into())}
            stage.file().write_all_at(&bytes,offset).map_err(|_|"worktree stage unavailable")?;offset=next;
        }Ok(())
    }

    #[cfg(target_os="linux")]
    fn finish_worktree_inverse_history(&self,stage:&crate::engine::WorktreeInverseFinishStage<'_>,deadline:std::time::Instant)->Result<(),String>{
        if self.actor_kind!="human" || self.actor_id!=stage.actor_id() || self.account_id!=stage.account_id(){return Err("worktree inverse completion unavailable".into())}
        stage.verify().map_err(|_|"worktree inverse journal unavailable")?;
        let normal=json!({"protocol_version":1,"auth":self.auth(),"action_id":stage.action_id()});
        let live=self.checkpoint_request(json!({"RecordLive":{"envelope":normal,"receipt":stage.live()}}),deadline)?;
        if live.pointer("/Action/action_id").and_then(Value::as_str)!=Some(stage.action_id()) || live.pointer("/Action/state").and_then(Value::as_str)!=Some("Applied") {return Err("worktree inverse outcome unavailable".into())}
        stage.verify().map_err(|_|"worktree inverse journal unavailable")?;
        let token=self.worktree_tokens.get(stage.root_id()).ok_or("worktree role unavailable")?;
        let role=json!({"protocol_version":1,"auth":{"actor_id":self.actor_id,"account_id":self.account_id,"token":token},"action_id":stage.action_id()});
        let result=self.checkpoint_request(json!({"FinalizeFilesWorktreeInverse":{"envelope":role,"root_id":stage.root_id(),"journal_operation_id":stage.operation_id()}}),deadline)?;
        if result.pointer("/Action/action_id").and_then(Value::as_str)!=Some(stage.action_id()) || result.pointer("/Action/state").and_then(Value::as_str)!=Some("Complete") {return Err("worktree inverse completion unavailable".into())}
        stage.verify().map_err(|_|"worktree inverse journal unavailable")?;Ok(())
    }

    fn managed_authorize_root(&self, path: &Path) -> Result<(String, PathBuf), String> {
        let (root, _) = self.managed_root_for(path)?;
        if root.kind != "recursive_directory" { return Err("managed moves require a directory root".into()); }
        Ok((root.root_id.clone(), root.canonical_path.clone()))
    }

    fn register_recoverable(
        &self, targets: &[CaptureTarget], checkpoint: &mut dyn FnMut(Value) -> Result<(), String>,
    ) -> Result<Vec<Value>, String> {
        if targets.is_empty() || targets.iter().any(|target| !self.allowed(&target.resource_id)) {
            return Err("target is outside the authorized workspace root".into());
        }
        let mut registrations = Vec::with_capacity(targets.len());
        for (index, target) in targets.iter().enumerate() {
            let pending = target.before.is_none() && self.root_for(&target.resource_id).is_err();
            let (detail, _) = self.register_resource_inner(&target.resource_id, pending)?;
            checkpoint(json!({"kind":"resource", "index":index, "detail":detail}))?;
            registrations.push(detail);
        }
        Ok(registrations)
    }

    fn graph_capture_active(&self) -> bool { self.graph_capture.get().is_some() }

    fn validate_graph_stage(&self,path:&Path,digest:&str,length:u64)->Result<(),String> {
        let Some(c)=self.graph_capture.get() else {return Ok(())};
        let target=path.to_str().filter(|v|path.is_absolute() && !v.is_empty()).ok_or("graph capture unavailable")?;
        if !self.allowed(path) || encode_hex(&Sha256::digest(target.as_bytes()))!=c.target_sha256
            || digest!=format!("sha256:{}",c.result_sha256) || length!=c.result_length {return Err("graph capture unavailable".into())}
        Ok(())
    }

    fn prepare(
        &self,
        operation: &str,
        targets: &[CaptureTarget],
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        if let Some(c)=self.graph_capture.get() {
            if operation!="create" || targets.len()!=1 || targets[0].before.is_some() {return Err("graph capture unavailable".into())}
            let target=targets[0].resource_id.to_str().filter(|_|targets[0].resource_id.is_absolute()).ok_or("graph capture unavailable")?;
            if encode_hex(&Sha256::digest(target.as_bytes()))!=c.target_sha256 {return Err("graph capture unavailable".into())}
            let timestamp=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64;
            let ticket=self.prepare_inner(operation,targets,c.action_id.clone(),timestamp,None,None,&[],None,None,None)?;
            *self.last_action.lock().map_err(|_|"graph capture unavailable")?=Some(c.action_id.clone());
            return Ok(ticket)
        }
        if self.workflow_writer.get().is_some() || self.ordinary_writer.get().is_some() {return Err("writer requires strict capture".into());}
        let action_id = self.next_action_id();
        let timestamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64;
        self.prepare_inner(operation, targets, action_id, timestamp, None, None, &[], None, None, None)
    }

    fn prepare_ordinary_write(
        &self, target: &CaptureTarget, binding: &OrdinaryWriteCapture, desired: &[u8],
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        self.validate_ordinary_binding(&target.resource_id, binding)?;
        if target.before.as_ref().is_none_or(|bytes| {
            std::str::from_utf8(bytes).is_err() || Self::fingerprint(Some(bytes)) != binding.expected_fingerprint
        }) || std::str::from_utf8(desired).is_err()
            || desired.len() as u64 != binding.desired_size
            || encode_hex(&Sha256::digest(desired)) != binding.desired_sha256 {
            return Err("ordinary write content changed".into());
        }
        self.prepare_inner("replace", std::slice::from_ref(target), binding.action_id.clone(),
            binding.timestamp_millis, None, None, &[], None, Some(&binding.request_digest), None)
    }

    fn ordinary_write_status(&self, path: &Path, binding: &OrdinaryWriteCapture) -> Result<Value, String> {
        self.validate_ordinary_binding(path, binding)?;
        let response = self.request(json!({"GetCaptureAction": {"protocol_version":1, "auth":self.auth(), "action_id":binding.action_id}}));
        let Ok(response) = response else { return Ok(json!({"state":"unknown"})); };
        let Some(action) = response.get("Action") else { return Ok(json!({"state":"unknown"})); };
        let path_text = path.to_str().ok_or_else(|| "ordinary write path unavailable".to_owned())?;
        let before = action.get("before_resources").and_then(Value::as_array);
        let live = action.get("live");
        let expected_after = format!("sha256:{}:{}", binding.desired_sha256, binding.desired_size);
        let exact_before = before.is_some_and(|items| items.len() == 1
            && items[0].pointer("/resource_key/account_id").and_then(Value::as_str) == Some(self.account_id.as_str())
            && items[0].pointer("/old_locator/location_label").and_then(Value::as_str) == Some(path_text)
            && items[0].get("fingerprint").and_then(Value::as_str) == Some(binding.expected_fingerprint.as_str())
            && items[0].get("resource_key") == action.get("resource_key"));
        let exact_live = live.is_some_and(|receipt| receipt.get("action_id").and_then(Value::as_str) == Some(binding.action_id.as_str())
            && receipt.get("status").and_then(Value::as_str) == Some("Committed")
            && receipt.get("fingerprint").and_then(Value::as_str) == Some(expected_after.as_str())
            && receipt.get("committed_resources").and_then(Value::as_array).is_some_and(|items| items.len() == 1
                && items[0].get("resource_key") == action.get("resource_key")
                && items[0].get("fingerprint").and_then(Value::as_str) == Some(expected_after.as_str())));
        if action.get("action_id").and_then(Value::as_str) == Some(binding.action_id.as_str())
            && action.get("actor_account_id").and_then(Value::as_str) == Some(self.account_id.as_str())
            && action.get("actor_id").and_then(Value::as_str) == Some(self.actor_id.as_str())
            && action.get("operation").and_then(Value::as_str) == Some("replace")
            && action.pointer("/coverage/metadata/ordinary_write_request_digest").and_then(Value::as_str) == Some(binding.request_digest.as_str())
            && action.get("state").and_then(Value::as_str) == Some("Complete") && exact_before && exact_live {
            if action["recovery_only"] == true && action["recovery_settled"] != true {
                let _ = self.settle_recovery(&binding.action_id);
            }
            Ok(json!({"state":"committed", "fingerprint":expected_after}))
        } else { Ok(json!({"state":"unknown"})) }
    }

    fn prepare_ordinary_create(&self, target: &CaptureTarget, binding: &OrdinaryCreateCapture, desired: &[u8]) -> Result<Box<dyn MutationCaptureTicket>, String> {
        self.validate_ordinary_create_binding(&target.resource_id,binding)?;
        if target.before.is_some() || std::str::from_utf8(desired).is_err() || desired.contains(&0) || desired.len() as u64 != binding.desired_size || encode_hex(&Sha256::digest(desired)) != binding.desired_sha256 { return Err("ordinary create content changed".into()); }
        self.prepare_inner("create",std::slice::from_ref(target),binding.action_id.clone(),binding.timestamp_millis,None,None,&[],None,Some(&binding.request_digest),None)
    }

    fn ordinary_create_status(&self, path: &Path, binding: &OrdinaryCreateCapture) -> Result<Value, String> {
        self.validate_ordinary_create_binding(path, binding)?;
        let response = self.request(json!({"GetCaptureAction": {"protocol_version":1, "auth":self.auth(), "action_id":binding.action_id}}));
        let Ok(response) = response else { return Ok(json!({"state":"unknown"})); };
        let Some(action) = response.get("Action") else { return Ok(json!({"state":"unknown"})); };
        let path_text = path.to_str().ok_or_else(|| "ordinary write path unavailable".to_owned())?;
        let before = action.get("before_resources").and_then(Value::as_array);
        let live = action.get("live");
        let expected_after = format!("sha256:{}:{}", binding.desired_sha256, binding.desired_size);
        let exact_before = before.is_some_and(|items| items.len() == 1
            && items[0].pointer("/resource_key/account_id").and_then(Value::as_str) == Some(self.account_id.as_str())
            && items[0].pointer("/old_locator/location_label").and_then(Value::as_str) == Some(path_text)
            && items[0].get("fingerprint").and_then(Value::as_str) == Some("missing")
            && items[0].get("resource_key") == action.get("resource_key")
            && items[0].get("existence").and_then(Value::as_str) == Some("Absent"));
        let exact_live = live.is_some_and(|receipt| receipt.get("action_id").and_then(Value::as_str) == Some(binding.action_id.as_str())
            && receipt.get("status").and_then(Value::as_str) == Some("Committed")
            && receipt.get("fingerprint").and_then(Value::as_str) == Some(expected_after.as_str())
            && receipt.get("committed_resources").and_then(Value::as_array).is_some_and(|items| items.len() == 1
                && items[0].get("resource_key") == action.get("resource_key")
                && items[0].get("fingerprint").and_then(Value::as_str) == Some(expected_after.as_str())));
        if action.get("action_id").and_then(Value::as_str) == Some(binding.action_id.as_str())
            && action.get("actor_account_id").and_then(Value::as_str) == Some(self.account_id.as_str())
            && action.get("actor_id").and_then(Value::as_str) == Some(self.actor_id.as_str())
            && action.get("operation").and_then(Value::as_str) == Some("create")
            && action.pointer("/coverage/metadata/ordinary_write_request_digest").and_then(Value::as_str) == Some(binding.request_digest.as_str())
            && action.get("state").and_then(Value::as_str) == Some("Complete") && exact_before && exact_live {
            if action["recovery_only"] == true && action["recovery_settled"] != true {
                let _ = self.settle_recovery(&binding.action_id);
            }
            Ok(json!({"state":"committed", "fingerprint":expected_after}))
        } else { Ok(json!({"state":"unknown"})) }
    }

    fn prepare_recoverable(
        &self, operation: &str, targets: &[CaptureTarget], action_id: &str, timestamp_millis: u64, registry_plan_digest: &str,
        checkpoint: &mut dyn FnMut(Value) -> Result<(), String>,
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        if action_id.is_empty() || action_id.len() > 128 || !action_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') {
            return Err("recoverable history action id is invalid".into());
        }
        if registry_plan_digest.len() != 64 || !registry_plan_digest.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) {
            return Err("recoverable registry plan digest is invalid".into());
        }
        self.prepare_inner(operation, targets, action_id.to_owned(), timestamp_millis, Some(registry_plan_digest), Some(checkpoint), &[], None, None, None)
    }

    fn prepare_worktree_action(&self,target:&CaptureTarget,binding:&WorktreeCaptureBinding,registrations:&[Value],registry_digest:&str,timestamp_millis:u64,checkpoint:&mut dyn FnMut(Value)->Result<(),String>)->Result<Box<dyn MutationCaptureTicket>,String>{
        let valid_oid=|value:&str| matches!(value.len(),40|64) && value.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c));
        if target.before.is_some() || !target.resource_id.is_absolute() || !valid_oid(&binding.commit_oid) || !valid_oid(&binding.tree_oid) || binding.commit_oid.len()!=binding.tree_oid.len()
            || binding.target_manifest_sha256.len()!=64 || !binding.target_manifest_sha256.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c)) {return Err("worktree capture unavailable".into())}
        let action=self.managed_action_id(&binding.operation_id)?;
        if registrations.len()!=1 || registry_digest.len()!=64 || !registry_digest.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c)) {return Err("worktree registration unavailable".into())}
        let directories=vec![target.resource_id.clone()];
        let provenance=json!({"version":2,"allowed_after_format":"indexed_tree_v2","commit_oid":binding.commit_oid,"tree_oid":binding.tree_oid,"target_manifest_sha256":binding.target_manifest_sha256,"journal_operation_id":binding.operation_id});
        self.prepare_inner("source_worktree_create_v2",std::slice::from_ref(target),action,timestamp_millis,Some(registry_digest),Some(checkpoint),&directories,Some(registrations),None,Some(&provenance))
    }

    fn register_checkout_targets(&self,targets:&[CaptureTarget],root_id:&str,operation_id:&str,directories:&[PathBuf],checkpoint:&mut dyn FnMut(Value)->Result<(),String>)->Result<Vec<Value>,String>{
        if targets.is_empty()||targets.len()>64||directories.len()>targets.len(){return Err("checkout registration unavailable".into())}
        let root=self.authorized_roots.iter().find(|r|r.root_id==root_id&&r.kind=="recursive_directory").ok_or("checkout root unavailable")?;
        let to_relative=|path:&Path|->Result<String,String>{let r=path.strip_prefix(&root.canonical_path).map_err(|_|"checkout target outside root")?;let s=r.to_str().ok_or("checkout target invalid")?;if s.is_empty()||s.split('/').any(|p|p.is_empty()||p=="."||p==".."){return Err("checkout target invalid".into())}Ok(s.to_owned())};
        let paths:Vec<String>=targets.iter().map(|t|to_relative(&t.resource_id)).collect::<Result<_,_>>()?;let dirs:Vec<String>=directories.iter().map(|p|to_relative(p)).collect::<Result<_,_>>()?;let action=self.managed_action_id(operation_id)?;
        let mut out=Vec::new();for (index,target) in targets.iter().enumerate(){if !self.allowed(&target.resource_id){return Err("checkout target unavailable".into())}
            let detail=if target.before.is_some(){self.register_resource_inner_selected(&target.resource_id,false,Some(root_id))?.0}else{
                let response=self.request(json!({"RegisterCheckoutAbsent":{"envelope":{"protocol_version":1,"auth":self.auth(),"action_id":action},"account_id":self.account_id,"workspace_id":self.workspace_id,"root_id":root_id,"root_path":root.canonical_path,"relative_path":paths[index],"operation_id":operation_id,"paths":paths,"directories":dirs}}))?;
                let handle=response.pointer("/Resource/handle").ok_or("checkout resource unavailable")?;let resource_id=handle.get("resource_id").and_then(Value::as_str).ok_or("checkout resource invalid")?;let generation=handle.get("generation").and_then(Value::as_u64).ok_or("checkout generation invalid")?;
                json!({"resource_id":resource_id,"generation":generation,"root_id":root_id,"root_path":root.canonical_path,"relative_path":paths[index]})
            };checkpoint(json!({"kind":"resource","index":index,"detail":detail}))?;out.push(detail);
        }Ok(out)
    }
    fn prepare_checkout_targets(&self,targets:&[CaptureTarget],directories:&[PathBuf],registrations:&[Value],action_id:&str,timestamp_millis:u64,registry_digest:&str,checkpoint:&mut dyn FnMut(Value)->Result<(),String>)->Result<Box<dyn MutationCaptureTicket>,String>{
        if targets.is_empty()||targets.len()>64||targets.len()!=registrations.len()||directories.iter().any(|p|!targets.iter().any(|t|t.resource_id==*p&&t.before.is_none()))||registry_digest.len()!=64{return Err("checkout capture unavailable".into())}
        self.prepare_inner("git_checkout",targets,action_id.to_owned(),timestamp_millis,Some(registry_digest),Some(checkpoint),directories,Some(registrations),None,None)
    }

    fn prepare_recoverable_directories(
        &self, operation: &str, targets: &[CaptureTarget], directories: &[PathBuf], registrations: &[Value], action_id: &str,
        timestamp_millis: u64, registry_plan_digest: &str, checkpoint: &mut dyn FnMut(Value) -> Result<(), String>,
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        if action_id.is_empty() || action_id.len() > 128 || !action_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
            || registry_plan_digest.len() != 64 || !registry_plan_digest.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            || directories.len() < 2 || directories.len() > targets.len() || directories[0] == directories[1]
            || directories.iter().collect::<std::collections::BTreeSet<_>>().len() != directories.len()
            || registrations.len() != targets.len()
            || directories.iter().any(|path| targets.iter().filter(|target| &target.resource_id == path).count() != 1)
            || !targets.iter().any(|target| target.resource_id == directories[0] && target.before.is_some())
            || !targets.iter().any(|target| target.resource_id == directories[1] && target.before.is_none()) {
            return Err("recoverable directory capture identity is invalid".into());
        }
        self.prepare_inner(operation, targets, action_id.to_owned(), timestamp_millis, Some(registry_plan_digest), Some(checkpoint), directories, Some(registrations), None, None)
    }

    fn register_recoverable_folder(
        &self, targets: &[CaptureTarget], source: &Path, destination: &Path, operation_id: &str,
        asset_proofs: &[Value],
        checkpoint: &mut dyn FnMut(Value) -> Result<(), String>,
    ) -> Result<Vec<Value>, String> {
        if targets.is_empty() || targets.len() > 128 || source.starts_with(destination) || destination.starts_with(source)
            || targets.iter().any(|target| !self.allowed(&target.resource_id)) {
            return Err("recoverable folder registration is invalid".into());
        }
        let (root, _) = self.root_for(source)?;
        let source_relative = source.strip_prefix(&root.canonical_path).map_err(|_| "folder source is outside its root")?;
        let (destination_root, _) = self.root_for(destination.parent().ok_or("folder destination is unavailable")?)?;
        let destination_relative = destination.strip_prefix(&destination_root.canonical_path).map_err(|_| "folder destination is outside its root")?;
        let cross = root.root_id != destination_root.root_id || root.canonical_path != destination_root.canonical_path;
        let action_id = self.managed_action_id(operation_id)?;
        let mut previous: HashMap<PathBuf, Value> = HashMap::new();
        let mut registrations = Vec::with_capacity(targets.len());
        for (index, target) in targets.iter().enumerate() {
            let structural_source = target.resource_id.strip_prefix(destination).ok().map(|suffix| source.join(suffix));
            let asset_proof = asset_proofs.iter().find(|proof|
                proof.get("destination").and_then(Value::as_str).map(Path::new) == Some(target.resource_id.as_path())
                    && proof.get("path").and_then(Value::as_str).map(Path::new) != structural_source.as_deref());
            let detail = if target.before.is_none() && (target.resource_id.starts_with(destination) || asset_proof.is_some()) {
                let source_path = if let Some(proof) = asset_proof {
                    PathBuf::from(proof.get("path").and_then(Value::as_str).ok_or("folder asset source is unavailable")?)
                } else {
                let suffix = target.resource_id.strip_prefix(destination).map_err(|_| "folder destination changed")?;
                    source.join(suffix)
                };
                let source_detail = previous.get(&source_path).ok_or("folder source registration is unavailable")?;
                let source_id = source_detail.get("resource_id").and_then(Value::as_str).ok_or("folder source identity is unavailable")?;
                let mut request = json!({"RegisterManagedFolderDestination": {
                    "envelope":{"protocol_version":1,"auth":self.auth(),"action_id":action_id},
                    "account_id":self.account_id,"workspace_id":self.workspace_id,"root_id":root.root_id,
                    "root_path":root.canonical_path,"operation_id":operation_id,"source_resource_id":source_id,
                    "source_folder":source_relative,"destination_folder":destination_relative,"asset_proof":asset_proof
                }});
                if cross {
                    request["RegisterManagedFolderDestination"]["destination_root_id"] = json!(destination_root.root_id);
                    request["RegisterManagedFolderDestination"]["destination_root_path"] = json!(destination_root.canonical_path);
                }
                let response = self.request(request)?;
                let handle = response.get("Resource").and_then(|value| value.get("handle")).ok_or("folder destination registration is unavailable")?;
                let resource_id = handle.get("resource_id").and_then(Value::as_str).filter(|value| !value.is_empty()).ok_or("folder destination identity is invalid")?;
                let generation = handle.get("generation").and_then(Value::as_u64).filter(|value| *value > 0).ok_or("folder destination generation is invalid")?;
                let target_root = if asset_proof.is_some() && !cross { root } else { destination_root };
                let relative = target.resource_id.strip_prefix(&target_root.canonical_path).map_err(|_| "folder destination is outside its root")?;
                json!({"resource_id":resource_id,"generation":generation,"root_id":target_root.root_id,
                    "root_path":target_root.canonical_path,"relative_path":relative})
            } else {
                let pending = target.before.is_none() && self.root_for(&target.resource_id).is_err();
                self.register_resource_inner(&target.resource_id, pending)?.0
            };
            checkpoint(json!({"kind":"resource","index":index,"detail":detail}))?;
            previous.insert(target.resource_id.clone(), detail.clone());
            registrations.push(detail);
        }
        Ok(registrations)
    }

    fn list_managed_folder_registrations(&self, source: &Path, destination: &Path) -> Result<Vec<Value>, String> {
        use std::path::Component;
        let (root, _) = self.root_for(source)?;
        let source_relative = source.strip_prefix(&root.canonical_path).map_err(|_| "folder source is outside its root")?;
        let (destination_root, _) = self.root_for(destination.parent().ok_or("folder destination is unavailable")?)?;
        let destination_relative = destination.strip_prefix(&destination_root.canonical_path).map_err(|_| "folder destination is outside its root")?;
        let cross = root.root_id != destination_root.root_id || root.canonical_path != destination_root.canonical_path;
        if source_relative.as_os_str().is_empty() || destination_relative.as_os_str().is_empty() { return Err("folder cannot be the root".into()); }
        let mut request = json!({"RegisteredSubtreeSnapshot": {
            "envelope":{"protocol_version":1,"auth":self.auth(),"action_id":"resource-registry"},
            "account_id":self.account_id,"workspace_id":self.workspace_id,"root_id":root.root_id,
            "root_path":root.canonical_path,"source_relative_path":source_relative,"destination_relative_path":destination_relative
        }});
        if cross {
            request["RegisteredSubtreeSnapshot"]["destination_root_id"] = json!(destination_root.root_id);
            request["RegisteredSubtreeSnapshot"]["destination_root_path"] = json!(destination_root.canonical_path);
        }
        let response = self.request(request)?;
        let items = response.get("RegisteredSubtreeSnapshot").and_then(|value| value.get("items"))
            .and_then(Value::as_array).filter(|items| items.len() <= 128).ok_or("folder registrations are unavailable")?;
        let mut seen = std::collections::BTreeSet::new();
        let mut result = Vec::new();
        for item in items {
                let path = item.get("relative_path").and_then(Value::as_str).ok_or("folder registration path is invalid")?;
                let path = Path::new(path);
                if path.is_absolute() || path.components().any(|part| !matches!(part, Component::Normal(_)))
                    || path.to_str().is_none_or(|value| value.contains('\\')) || !path.starts_with(source_relative) {
                    return Err("folder registration path is unsupported".into());
                }
                let id = item.get("resource_id").and_then(Value::as_str).filter(|value| !value.is_empty()).ok_or("folder registration identity is invalid")?;
                if item.get("generation").and_then(Value::as_u64).is_none_or(|value| value == 0) || !seen.insert(id.to_owned()) {
                    return Err("folder registration changed during discovery".into());
                }
                result.push(item.clone());
        }
        Ok(result)
    }

    fn managed_action_id(&self, operation_id: &str) -> Result<String, String> {
        if operation_id.len() < 16 || operation_id.len() > 80 || !operation_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') {
            return Err("managed operation id is invalid".into());
        }
        let binding = format!("{}\0{}\0{}", self.account_id, self.actor_id, operation_id);
        Ok(format!("odysseus-managed-{}", encode_hex(&Sha256::digest(binding.as_bytes()))))
    }

    fn recoverable_action(&self, action_id: &str, operation_id: &str) -> Result<Value, String> {
        if self.managed_action_id(operation_id)? != action_id { return Err("recoverable history action identity changed".into()); }
        self.request(json!({"LookupManagedAction": {"envelope":{"protocol_version":1, "auth":self.auth(), "action_id":action_id}, "operation_id":operation_id}}))
    }

    fn plan_recoverable_completion(
        &self, action_id: &str, prepared: &Value, after: &[CaptureAfter],
    ) -> Result<(Value, Vec<(String, Vec<u8>, String)>), String> {
        if prepared.get("kind").and_then(Value::as_str) != Some("prepare_batch")
            || prepared.pointer("/request/action_id").and_then(Value::as_str) != Some(action_id) {
            return Err("recoverable prepare identity changed".into());
        }
        let before = prepared.get("entries").and_then(Value::as_array)
            .filter(|entries| entries.len() == after.len() && !entries.is_empty())
            .ok_or_else(|| "recoverable completion cardinality changed".to_owned())?;
        let mut entries = Vec::with_capacity(after.len()); let mut stages = Vec::new(); let mut inline = 0usize;
        for (index, (item, prior)) in after.iter().zip(before).enumerate() {
            let resource = prior.get("resource_key").ok_or_else(|| "recoverable resource identity is missing".to_owned())?;
            let resource_id = resource.get("resource_id").and_then(Value::as_str).ok_or_else(|| "recoverable resource identity is invalid".to_owned())?;
            if resource.get("account_id").and_then(Value::as_str) != Some(self.account_id.as_str())
                || resource.get("workspace_id").and_then(Value::as_str) != Some(self.workspace_id.as_str())
                || prior.pointer("/old_locator/location_label").and_then(Value::as_str) != Some(item.resource_id.to_string_lossy().as_ref()) {
                return Err("recoverable resource scope or path changed".into());
            }
            let fingerprint = Self::fingerprint(item.after.as_deref());
            let (resource_type, metadata) = match fs::symlink_metadata(&item.resource_id) {
                Ok(stat) if stat.file_type().is_symlink() => ("Symlink", json!({"mode":mode(&stat), "size":stat.len(), "modified_millis":modified_millis(&stat), "opaque":{"target":fs::read_link(&item.resource_id).ok().map(|value| value.to_string_lossy().into_owned())}})),
                Ok(stat) if stat.is_dir() => ("Directory", json!({"mode":mode(&stat), "size":null, "modified_millis":modified_millis(&stat), "opaque":{"manifest_digest":item.after.as_deref().map(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)))}})),
                Ok(stat) => ("File", json!({"mode":mode(&stat), "size":stat.len(), "modified_millis":modified_millis(&stat), "opaque":null})),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound && item.after.is_none()
                    && prior.get("resource_type").and_then(Value::as_str) == Some("Directory") =>
                    ("Directory", json!({"mode":null, "size":null, "modified_millis":null, "opaque":null})),
                Err(_) => ("File", json!({"mode":null, "size":null, "modified_millis":null, "opaque":null})),
            };
            let after_digest = item.after.as_deref().map(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)));
            let mut entry = json!({"resource_key":resource, "locator":{"display_name":item.resource_id.file_name().map(|name| name.to_string_lossy()).unwrap_or_default(), "location_label":item.resource_id.to_string_lossy(), "opaque_ref":resource_id},
                "existence":if item.after.is_some() {"Present"} else {"Absent"}, "resource_type":resource_type, "metadata":metadata,
                "content":item.after.as_deref().map(encode_base64), "staged_upload_id":null, "fingerprint":fingerprint,
                "coverage":{"byte_len":item.after.as_ref().map_or(0, |bytes| bytes.len()), "content_digest":after_digest, "metadata":{"exact_after":true}},
                "outcome":{"resource_id":resource_id, "status":"Committed", "revision":{"Opaque":{"kind":"fingerprint", "value":fingerprint}}}});
            if let Some(bytes) = item.after.as_deref() {
                if bytes.len() > INLINE_CONTENT_BYTES || inline.saturating_add(bytes.len()) > STAGE_CHUNK_BYTES {
                    let key = format!("{}\0after\0{}\0{}", action_id, index, fingerprint);
                    let id = format!("odysseus-stage-{}", encode_hex(&Sha256::digest(key.as_bytes())[..24]));
                    entry["content"] = Value::Null; entry["staged_upload_id"] = Value::String(id.clone());
                    stages.push((id, bytes.to_vec(), fingerprint));
                } else { inline = inline.saturating_add(bytes.len()); }
            }
            entries.push(entry);
        }
        let committed: Vec<Value> = entries.iter().map(|entry| { let mut proof=entry.clone(); for field in ["content", "staged_upload_id", "outcome"] { proof.as_object_mut().unwrap().remove(field); } proof }).collect();
        let live = json!({"action_id":action_id, "status":"Committed", "fingerprint":entries.first().and_then(|entry| entry.get("fingerprint")), "committed_resources":committed});
        Ok((json!({"live":live, "entries":entries}), stages))
    }

    fn complete_recoverable(
        &self, action_id: &str, completion: &Value, stages: &[(String, Vec<u8>, String)], already_complete: bool,
    ) -> Result<Value, String> {
        let object = completion.as_object().ok_or_else(|| "invalid history completion".to_owned())?;
        if object.len() != 2 || !object.contains_key("live") || !object.contains_key("entries") {
            return Err("invalid history completion".into());
        }
        let live = &object["live"];
        if live.get("action_id").and_then(Value::as_str) != Some(action_id) {
            return Err("history completion action changed".into());
        }
        let entries = object["entries"].as_array().filter(|value| !value.is_empty() && value.len() <= 1024)
            .ok_or_else(|| "invalid history completion entries".to_owned())?;
        let mut expected = HashMap::new();
        for entry in entries {
            if entry.pointer("/resource_key/account_id").and_then(Value::as_str) != Some(self.account_id.as_str()) {
                return Err("history completion account changed".into());
            }
            if let Some(id) = entry.get("staged_upload_id").and_then(Value::as_str) {
                let fingerprint = entry.get("fingerprint").and_then(Value::as_str).ok_or_else(|| "history stage fingerprint is missing".to_owned())?;
                if expected.insert(id.to_owned(), fingerprint.to_owned()).is_some() {
                    return Err("history stage identity is duplicated".into());
                }
            }
        }
        if stages.len() != expected.len() { return Err("history stages are incomplete".into()); }
        for (id, bytes, fingerprint) in stages {
            if expected.remove(id).as_deref() != Some(fingerprint.as_str()) || Self::fingerprint(Some(bytes)).as_str() != fingerprint.as_str() {
                return Err("history staged bytes changed".into());
            }
        }
        if !already_complete {
            self.request(json!({"RecordLive": {"envelope": {"protocol_version":1, "auth":self.auth(), "action_id":action_id}, "receipt":live}}))?;
            for (id, bytes, fingerprint) in stages {
                self.stage_content_with_id(action_id, bytes, fingerprint, id.clone(), true)?;
            }
        }
        self.request(json!({"CompleteBatch": {"envelope": {"protocol_version":1, "auth":self.auth(), "action_id":action_id}, "batch_version":1, "entries":entries}}))
    }

    fn commit_recoverable_registry(&self, action_id: &str, plan: &Value) -> Result<(), String> {
        let (moves, guard) = if let Some(items) = plan.as_array() {
            (items, None)
        } else if let Some(object) = plan.as_object().filter(|object| object.len() == 2) {
            let items = object.get("moves").and_then(Value::as_array).ok_or_else(|| "recoverable registry plan is invalid".to_owned())?;
            let guard = object.get("subtree_guard").filter(|value| value.is_object()).ok_or_else(|| "recoverable registry plan is invalid".to_owned())?;
            (items, Some(guard))
        } else { return Err("recoverable registry plan is invalid".into()); };
        if moves.is_empty() || moves.len() > 128 {
            return Err("recoverable registry plan is invalid".into());
        }
        let mut request = json!({"MoveResourcesBatch": {
            "envelope":{"protocol_version":1, "auth":self.auth(), "action_id":action_id},
            "account_id":self.account_id, "workspace_id":self.workspace_id, "moves":moves
        }});
        if let Some(guard) = guard { request["MoveResourcesBatch"]["subtree_guard"] = guard.clone(); }
        let response = self.request(request).map_err(|error| {
            let code = serde_json::from_str::<Value>(&error).ok()
                .and_then(|value| value.pointer("/Error/code").and_then(Value::as_str).map(str::to_owned));
            if guard.is_some() && code.as_deref() == Some("registry batch changed; review required") {
                "managed_registry_subtree_changed".to_owned()
            } else { error }
        })?;
        if response == Value::String("Accepted".into()) || response.get("Accepted").is_some() { Ok(()) }
        else { Err("recoverable registry outcome is unknown".into()) }
    }

    fn validate_recoverable_subtree(&self, action_id: &str, plan: &Value) -> Result<(), String> {
        let object = plan.as_object().filter(|object| object.len() == 2)
            .ok_or_else(|| "recoverable subtree plan is invalid".to_owned())?;
        let moves = object.get("moves").and_then(Value::as_array).filter(|items| !items.is_empty() && items.len() <= 128)
            .ok_or_else(|| "recoverable subtree plan is invalid".to_owned())?;
        let guard = object.get("subtree_guard").filter(|value| value.is_object())
            .ok_or_else(|| "recoverable subtree plan is invalid".to_owned())?;
        let response = self.request(json!({"MoveResourcesBatch": {
            "envelope":{"protocol_version":1, "auth":self.auth(), "action_id":action_id},
            "account_id":self.account_id, "workspace_id":self.workspace_id, "moves":moves,
            "subtree_guard":guard, "validate_only":true
        }})).map_err(|error| {
            let code = serde_json::from_str::<Value>(&error).ok()
                .and_then(|value| value.pointer("/Error/code").and_then(Value::as_str).map(str::to_owned));
            if code.as_deref() == Some("registry batch changed; review required") {
                "managed_registry_subtree_changed".to_owned()
            } else { error }
        })?;
        if response == Value::String("Accepted".into()) || response.get("Accepted").is_some() { Ok(()) }
        else { Err("recoverable subtree validation is unknown".into()) }
    }

    fn status(&self, action_id: &str) -> Option<CaptureStatus> {
        self.statuses.lock().ok()?.get(action_id).cloned()
    }

    fn last_action_id(&self) -> Option<String> {
        self.last_action.lock().ok()?.clone()
    }
}

impl HistoryServiceHook {
    fn validate_ordinary_create_binding(&self,path:&Path,binding:&OrdinaryCreateCapture)->Result<(),String> {
        self.validate_workflow_writer(path,&binding.operation_id,&binding.request_digest)?;
        if let Some(c)=self.ordinary_writer.get() {
            let target=path.to_str().filter(|_|path.is_absolute()).ok_or("ordinary writer unavailable")?;
            if c.canonical_tool!="write_file" || c.operation_id!=binding.operation_id || c.request_digest!=binding.request_digest ||
                c.target_sha256!=encode_hex(&Sha256::digest(target.as_bytes())) || !self.allowed(path) {
                return Err("ordinary writer unavailable".into());
            }
        }
        fn hex(s:&str)->bool {s.len()==64 && s.bytes().all(|c|c.is_ascii_digit() || (b'a'..=b'f').contains(&c))}
        fn id(s:&str)->bool {!s.is_empty() && s.len()<=128 && s.bytes().all(|c|c.is_ascii_alphanumeric() || c==b'-' || c==b'_')}
        let path=path.to_str().filter(|s|!s.is_empty()).ok_or_else(||"ordinary create path unavailable".to_owned())?;
        if !id(&binding.operation_id) || !id(&binding.action_id) || !hex(&binding.request_digest) || !hex(&binding.desired_sha256) || !binding.parent_revision.strip_prefix("sha256:").is_some_and(hex) {return Err("ordinary create identity invalid".into());}
        let value=json!(["openclank.files.ordinary-create.v1",self.account_id,path,binding.parent_revision,binding.desired_sha256,binding.desired_size,binding.operation_id,binding.action_id]);
        if encode_hex(&Sha256::digest(serde_json::to_vec(&value).map_err(|_|"ordinary create identity invalid".to_owned())?))!=binding.request_digest {return Err("ordinary create identity changed".into());}
        Ok(())
    }

    fn validate_ordinary_binding(&self, path: &Path, binding: &OrdinaryWriteCapture) -> Result<(), String> {
        self.validate_workflow_writer(path,&binding.operation_id,&binding.request_digest)?;
        if let Some(c)=self.ordinary_writer.get() {
            let target=path.to_str().filter(|_|path.is_absolute()).ok_or("ordinary writer unavailable")?;
            if c.operation_id!=binding.operation_id || c.request_digest!=binding.request_digest ||
                c.target_sha256!=encode_hex(&Sha256::digest(target.as_bytes())) || !self.allowed(path) {
                return Err("ordinary writer unavailable".into());
            }
        }
        fn lower_hex(s: &str) -> bool { s.len() == 64 && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) }
        fn identifier(s: &str) -> bool { !s.is_empty() && s.len() <= 128 && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') }
        let path = path.to_str().filter(|s| !s.is_empty()).ok_or_else(|| "ordinary write path unavailable".to_owned())?;
        if !identifier(&binding.operation_id) || !identifier(&binding.action_id) || !lower_hex(&binding.request_digest)
            || !lower_hex(&binding.desired_sha256) || binding.expected_fingerprint.len() > 128 {
            return Err("ordinary write identity invalid".into());
        }
        let value = json!(["openclank.files.ordinary-replace.v1", self.account_id, path,
            binding.expected_fingerprint, binding.desired_sha256, binding.operation_id, binding.action_id]);
        let encoded = serde_json::to_vec(&value).map_err(|_| "ordinary write identity invalid".to_owned())?;
        if encode_hex(&Sha256::digest(encoded)) != binding.request_digest {
            return Err("ordinary write identity changed".into());
        }
        Ok(())
    }

    fn prepare_inner(
        &self, operation: &str, targets: &[CaptureTarget], action_id: String, timestamp_millis: u64, registry_plan_digest: Option<&str>,
        mut checkpoint: Option<&mut dyn FnMut(Value) -> Result<(), String>>, directories: &[PathBuf], pre_registered: Option<&[Value]>, ordinary_digest: Option<&str>, worktree_provenance: Option<&Value>,
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        if targets.is_empty()
            || targets
                .iter()
                .any(|target| !self.allowed(&target.resource_id))
        {
            self.set_status(
                &action_id,
                "paused",
                "excluded",
                Some("target is outside the authorized workspace root".into()),
            );
            return Err("target is outside the authorized workspace root".into());
        }
        let mut resource_ids = Vec::with_capacity(targets.len());
        let mut created_resource_ids: Vec<String> = Vec::new();
        for (index, target) in targets.iter().enumerate() {
            let pending = checkpoint.is_some() && target.before.is_none() && self.root_for(&target.resource_id).is_err();
            let registration = match pre_registered {
                Some(items) => items.get(index).cloned().map(|detail| (detail, false)).ok_or_else(|| "history registration identity is unavailable".to_owned()),
                None => self.register_resource_inner(&target.resource_id, pending),
            };
            let (detail, created) = match registration {
                Ok(result) => result,
                Err(error) => {
                    self.set_status(&action_id, "paused", "registry", Some(error.clone()));
                    if checkpoint.is_none() {
                        for resource_id in &created_resource_ids {
                            let _ = self.revoke_resource(resource_id, "resource registration failed");
                        }
                    }
                    return Err(error);
                }
            };
            let resource_id = detail.get("resource_id").and_then(Value::as_str)
                .ok_or_else(|| "history registration identity is missing".to_owned())?.to_owned();
            if created {
                created_resource_ids.push(resource_id.clone());
            }
            if let Some(record) = checkpoint.as_deref_mut() {
                record(json!({"kind":"resource", "index":index, "detail":detail}))?;
            }
            resource_ids.push(resource_id);
        }
        let registry_commit = match operation {
            "move" | "rename" | "restore" if targets.len() > 1 => Some(RegistryCommit::Move {
                source_id: resource_ids[0].clone(),
                destination_id: resource_ids[1].clone(),
                destination: self.canonical_target(&targets[1].resource_id)?,
            }),
            "trash" => Some(RegistryCommit::Revoke {
                resource_id: resource_ids[0].clone(),
            }),
            _ => None,
        };
        let modified = targets
            .iter()
            .zip(resource_ids.iter())
            .map(|(_target, resource_id)| {
                json!({
                    "account_id": self.account_id,
                    "workspace_id": self.workspace_id,
                    "provider": "odysseus-files",
                    "resource_id": resource_id,
                })
            })
            .collect::<Vec<_>>();
        let mut request = json!({
            "schema_version": 1,
            "action_id": action_id,
            "actor_account_id": self.account_id,
            "resource_key": modified[0],
            "guard_resource_ids": [],
            "modified_resource_ids": modified,
            "operation": operation,
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind,
            "tool_id": "odysseus-files",
            "timestamp_millis": timestamp_millis,
            "coverage": {"metadata": {"coverage_kind": if targets.len() > 1 { "Partial" } else { "KnownMutationHooks" }, "roots": self.authorized_roots.iter().map(|root| format!("root:{}", root.root_id)).collect::<Vec<_>>(), "exclusions": if targets.len() > 1 { vec!["non-primary targets use resource outcomes"] } else { Vec::<&str>::new() }}},
            "per_resource_outcomes": null,
        });
        if self.mark_recovery { request["coverage"]["metadata"]["required_recovery"] = json!(true); }
        if let Some(provenance)=worktree_provenance {
            if operation!="source_worktree_create_v2" || targets.len()!=1 || targets[0].before.is_some() || directories!=[targets[0].resource_id.clone()] {return Err("worktree capture unavailable".into())}
            request["coverage"]["metadata"]["source_worktree_tree_v2"]=provenance.clone();
        }
        if let Some(digest) = registry_plan_digest {
            request["coverage"]["metadata"]["managed_registry_plan_digest"] = json!(digest);
        }
        if let Some(digest) = ordinary_digest {
            request["coverage"]["metadata"]["ordinary_write_request_digest"] = json!(digest);
            if let Some(writer)=self.workflow_writer.get() {
                if writer.request_digest!=digest {return Err("workflow writer unavailable".into());}
                request["tool_id"]=json!("workflow_artifact");
                request["coverage"]["metadata"]["writerIdentity"]=writer.writer_metadata();
            }
            if let Some(writer)=self.ordinary_writer.get() {
                if writer.request_digest!=digest || targets.len()!=1 || !(operation=="replace" || (operation=="create" && writer.canonical_tool=="write_file" && targets[0].before.is_none())) {return Err("ordinary writer unavailable".into());}
                request["tool_id"]=json!(writer.canonical_tool);
                request["managed_tool_origin_v1"]=serde_json::to_value(&writer.managed_tool_origin_v1).map_err(|_|"ordinary writer unavailable")?;
                request["coverage"]["metadata"]["ordinary_invocation_digest"]=json!(writer.invocation_digest);
            }
        }
        let graph_digest = if let Some(c)=self.graph_capture.get() {
            if operation!="create" || targets.len()!=1 || targets[0].before.is_some() || ordinary_digest.is_some() {return Err("graph capture unavailable".into())}
            request["tool_id"]=json!("graph_topology_capture");
            request["coverage"]["metadata"]["graph_topology_capture_v1"]=serde_json::to_value(c).map_err(|_|"graph capture unavailable")?;
            Some(c.request_sha256.as_str())
        } else {None};
        let once = operation == "create" && (ordinary_digest.is_some() || graph_digest.is_some());
        let once_marker = ordinary_digest.or(graph_digest).filter(|_| once).map(|digest| json!({"version":1,"binding_sha256":digest}));
        if let Some(marker)=once_marker.as_ref() {request["coverage"]["metadata"]["batch_once"]=marker.clone();}
        let envelope = json!({
            "protocol_version": 1,
            "auth": self.auth(),
            "claimed_digest": "",
            "request": request,
        });
        let mut wire_entries = Vec::with_capacity(targets.len());
        let mut staged_uploads = Vec::new();
        let mut inline_bytes = 0usize;
        for (index, (target, resource_id)) in targets.iter().zip(resource_ids.iter()).enumerate() {
            let fingerprint = Self::fingerprint(target.before.as_deref());
            let expected_directory = directories.contains(&target.resource_id);
            let stat = fs::symlink_metadata(&target.resource_id);
            if expected_directory && stat.as_ref().is_ok_and(|metadata| !metadata.is_dir() || target.before.is_none()) {
                return Err("directory capture target changed type".into());
            }
            let (resource_type, metadata) = match stat {
                Ok(stat) if stat.file_type().is_symlink() => (
                    "Symlink",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Ok(stat) if stat.is_dir() => (
                    "Directory",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Ok(stat) => (
                    "File",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Err(error) if expected_directory && error.kind() == std::io::ErrorKind::NotFound && target.before.is_none() =>
                    ("Directory", json!({"mode": null, "size": null, "modified_millis": null, "opaque": null})),
                Err(error) if expected_directory => return Err(format!("directory capture target is unavailable: {error}")),
                Err(_) => ("File", json!({"mode": null, "size": null, "modified_millis": null, "opaque": null})),
            };
            let mut entry = json!({
                "resource_key": {
                    "account_id": self.account_id,
                    "workspace_id": self.workspace_id,
                    "provider": "odysseus-files",
                    "resource_id": resource_id,
                },
                "old_locator": {"display_name": target.resource_id.file_name().map(|v| v.to_string_lossy()).unwrap_or_default(), "location_label": target.resource_id.to_string_lossy(), "opaque_ref": resource_id},
                "new_locator": {"display_name": target.resource_id.file_name().map(|v| v.to_string_lossy()).unwrap_or_default(), "location_label": target.resource_id.to_string_lossy(), "opaque_ref": resource_id},
                "expected_revision": {"Opaque": {"kind": "fingerprint", "value": fingerprint}},
                "existence": if target.before.is_some() { "Present" } else { "Absent" },
                "resource_type": resource_type,
                "metadata": metadata,
                "content": target.before.as_deref().map(encode_base64),
                "staged_upload_id": null,
                "fingerprint": fingerprint,
                "coverage": {"byte_len": target.before.as_ref().map_or(0, |bytes| bytes.len()), "metadata": {"exact_preimage": true}},
            });
            if let Some(marker)=once_marker.as_ref() {entry["metadata"]["opaque"]=marker.clone();}
            if resource_type == "Directory" {
                if let Some(content) = target.before.as_deref() {
                    let manifest_digest = format!("sha256:{:x}", Sha256::digest(content));
                    entry["metadata"]["opaque"] = json!({"manifest_digest": manifest_digest});
                    entry["coverage"]["content_digest"] = json!(manifest_digest);
                }
            }
            if let Some(content) = target.before.as_deref() {
                if content.len() > INLINE_CONTENT_BYTES || inline_bytes.saturating_add(content.len()) > STAGE_CHUNK_BYTES {
                    let upload_id = if let Some(record) = checkpoint.as_deref_mut() {
                        let key = format!("{}\0{}\0{}", action_id, index, fingerprint);
                        let digest = Sha256::digest(key.as_bytes());
                        let id = format!("odysseus-stage-{}", encode_hex(&digest[..24]));
                        record(json!({"kind":"stage", "index":index, "upload_id":id, "fingerprint":fingerprint}))?;
                        self.stage_content_with_id(&action_id, content, &fingerprint, id, true)?
                    } else {
                        self.stage_content(&action_id, content, &fingerprint)?
                    };
                    entry["content"] = Value::Null;
                    entry["staged_upload_id"] = Value::String(upload_id.clone());
                    staged_uploads.push(upload_id);
                } else {
                    inline_bytes = inline_bytes.saturating_add(content.len());
                }
            }
            wire_entries.push(entry);
        }
        if let Some(record) = checkpoint.as_deref_mut() {
            record(json!({"kind":"prepare_batch", "request":request, "batch_version":1, "entries":wire_entries}))?;
        }
        let prepared = if once {
            self.request(json!({"PrepareBatchOnce": {"envelope": envelope, "batch_version": 1, "entries": wire_entries}})).and_then(|value| {
                if value.pointer("/BatchOnce/created").and_then(Value::as_bool)==Some(true) {Ok(value)}
                else {Err("original create already issued or unavailable; status only".into())}
            })
        } else {
            self.request(json!({"PrepareBatch": {"envelope": envelope, "batch_version": 1, "entries": wire_entries}}))
        };
        if prepared.is_err() && checkpoint.is_none() {
            for upload_id in &staged_uploads {
                let _ = self.request(json!({"StageAbort": {"envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id}, "upload_id": upload_id}}));
            }
        }
        if let Err(error) = prepared {
            if checkpoint.is_none() {
                for resource_id in &created_resource_ids {
                    let _ = self.revoke_resource(resource_id, "history prepare failed");
                }
            }
            return Err(error);
        }
        self.set_status(&action_id, "prepared", "before_durable", None);
        Ok(Box::new(ServiceTicket {
            hook: Arc::new(self.clone()),
            action_id,
            created_resource_ids,
            registry_commit,
            resource_ids,
            physical_paths: targets
                .iter()
                .map(|target| target.resource_id.clone())
                .collect(),
        }))
    }

}

struct ServiceTicket {
    hook: Arc<HistoryServiceHook>,
    action_id: String,
    created_resource_ids: Vec<String>,
    registry_commit: Option<RegistryCommit>,
    resource_ids: Vec<String>,
    physical_paths: Vec<PathBuf>,
}

impl MutationCaptureTicket for ServiceTicket {
    fn action_id(&self) -> &str {
        &self.action_id
    }

    fn complete(self: Box<Self>, after: Vec<CaptureAfter>) -> Result<(), String> {
        let result: Result<(), String> = (|| {
        let auth = self.hook.auth();
        let control = json!({"protocol_version": 1, "auth": auth, "action_id": self.action_id});
        if after.len() != self.resource_ids.len() { return Err("history after-state cardinality mismatch".into()); }
        let mut complete_entries = Vec::with_capacity(after.len());
        let mut staged_uploads = Vec::new();
        for ((item, resource_id), physical_path) in after
            .iter()
            .zip(self.resource_ids.iter())
            .zip(self.physical_paths.iter())
        {
            let fingerprint = HistoryServiceHook::fingerprint(item.after.as_deref());
            let (resource_type, metadata) = match fs::symlink_metadata(physical_path) {
                Ok(stat) if stat.file_type().is_symlink() => (
                    "Symlink",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": {"target": fs::read_link(physical_path).ok().map(|target| target.to_string_lossy().into_owned())}}),
                ),
                Ok(stat) if stat.is_dir() => (
                    "Directory",
                    json!({"mode": mode(&stat), "size": null, "modified_millis": modified_millis(&stat), "opaque": {"manifest_digest": item.after.as_deref().map(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)))}}),
                ),
                Ok(stat) => (
                    "File",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Err(_) => ("File", json!({"mode": null, "size": null, "modified_millis": null, "opaque": null})),
            };
            let after_digest = item
                .after
                .as_deref()
                .map(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)));
            let entry = json!({
                "resource_key": {"account_id": self.hook.account_id, "workspace_id": self.hook.workspace_id, "provider": "odysseus-files", "resource_id": resource_id},
                "locator": {"display_name": physical_path.file_name().map(|name| name.to_string_lossy()).unwrap_or_default(), "location_label": physical_path.to_string_lossy(), "opaque_ref": resource_id},
                "existence": if item.after.is_some() { "Present" } else { "Absent" },
                "resource_type": resource_type,
                "metadata": metadata,
                "content": item.after.as_deref().map(encode_base64),
                "staged_upload_id": null,
                "fingerprint": fingerprint,
                "coverage": {"byte_len": item.after.as_ref().map_or(0, |bytes| bytes.len()), "content_digest": after_digest, "metadata": {"exact_after": true}},
                "outcome": {"resource_id": resource_id, "status": "Committed", "revision": {"Opaque": {"kind": "fingerprint", "value": fingerprint}}},
            });
            complete_entries.push(entry);
        }
        // Journal every terminal resource proof before any optional payload upload.
        let committed_resources: Vec<Value> = complete_entries.iter().map(|entry| {
            let mut proof = entry.clone();
            for field in ["content", "staged_upload_id", "outcome"] { proof.as_object_mut().unwrap().remove(field); }
            proof
        }).collect();
        let live = json!({"action_id": self.action_id, "status": "Committed",
            "fingerprint": complete_entries.first().and_then(|entry| entry.get("fingerprint")),
            "committed_resources": committed_resources});
        self.hook.request(json!({"RecordLive": {"envelope": control, "receipt": live}}))?;
        let mut registry_error = None;
        if let Some(commit) = self.registry_commit.as_ref() {
            self.hook.write_registry_journal(&self.action_id, commit)?;
        }
        for (entry, item) in complete_entries.iter_mut().zip(after.iter()) {
            let fingerprint = entry["fingerprint"].as_str().unwrap_or("missing").to_owned();
            if let Some(content) = item.after.as_deref() {
                if content.len() > INLINE_CONTENT_BYTES {
                    let upload_id = self.hook.stage_content(&self.action_id, content, &fingerprint)?;
                    entry["content"] = Value::Null;
                    entry["staged_upload_id"] = Value::String(upload_id.clone());
                    staged_uploads.push(upload_id);
                }
            }
        }
        let result = self.hook.request(json!({"CompleteBatch": {"envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}, "batch_version": 1, "entries": complete_entries}}));
        if result.is_err() {
            for upload_id in &staged_uploads {
                let _ = self.hook.request(json!({"StageAbort": {"envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}, "upload_id": upload_id}}));
            }
        }
        if result.is_ok() {
            if let Some(commit) = self.registry_commit.as_ref() {
                let registry_result = self
                    .hook
                    .write_registry_journal(&self.action_id, commit)
                    .and_then(|journal| {
                        self.hook
                            .apply_registry_commit(commit)
                            .and_then(|_| fs::remove_file(journal).map_err(|error| error.to_string()))
                    });
                if let Err(error) = registry_result {
                    self.hook.set_status(
                        &self.action_id,
                        "pending",
                        "registry-reconcile",
                        Some(error.clone()),
                    );
                    registry_error = Some(error);
                }
            }
        }
        match result {
            Ok(_) if registry_error.is_some() => self.hook.set_status(
                &self.action_id,
                "failed",
                "registry-reconcile",
                registry_error.clone(),
            ),
            Ok(reply) if reply.pointer("/Action/recovery_only").and_then(Value::as_bool) == Some(true) => {
                self.hook.set_status(&self.action_id, "skipped", "complete", None);
            }
            Ok(_) => self.hook.set_status(&self.action_id, "complete", "complete", None),
            Err(error) => self
                .hook
                .set_status(&self.action_id, "failed", "after", Some(error)),
        }
        self.hook
            .status(&self.action_id)
            .filter(|status| status.phase == "complete")
            .map(|_| ())
            .ok_or_else(|| "history after capture failed".into())
        })();
        if result.is_ok() {
            let _ = self.hook.settle_recovery(&self.action_id);
        }
        if let Err(error) = &result {
            if let Some(commit) = self.registry_commit.as_ref() {
                let _ = self.hook.write_registry_journal(&self.action_id, commit);
            }
            // If snapshot assembly failed before proof publication, retain the
            // known committed outcome without inventing missing original proof.
            let _ = self.hook.request(json!({"RecordLive": {
                "envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id},
                "receipt": {"action_id": self.action_id, "status": "Committed"}
            }}));
            if self.hook.request(json!({"AfterUnavailable": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}})).is_ok() {
                let _ = self.hook.settle_recovery(&self.action_id);
            }
            self.hook.set_status(&self.action_id, "failed", "after", Some(error.clone()));
        }
        result
    }

    fn after_unavailable(self: Box<Self>, error: String) {
        let _ = self.hook.request(json!({"RecordLive": {
            "envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id},
            "receipt": {"action_id": self.action_id, "status": "Committed", "after_unavailable": true}
        }}));
        if let Some(commit) = self.registry_commit.as_ref() {
            let _ = self.hook.write_registry_journal(&self.action_id, commit);
        }
        if self.hook.request(json!({"AfterUnavailable": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}})).is_ok() {
            let _ = self.hook.settle_recovery(&self.action_id);
        }
        self.hook.set_status(&self.action_id, "failed", "after", Some(error));
    }

    fn abort(self: Box<Self>) {
        if self.hook.request(json!({"Abort": {"envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}}})).is_ok() {
            let _ = self.hook.settle_recovery(&self.action_id);
        }
        for resource_id in &self.created_resource_ids {
            let _ = self
                .hook
                .revoke_resource(resource_id, "live mutation aborted");
        }
        self.hook
            .set_status(&self.action_id, "aborted", "aborted", None);
    }
}

fn encode_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let first = chunk[0] as u32;
        let second = chunk.get(1).copied().unwrap_or(0) as u32;
        let third = chunk.get(2).copied().unwrap_or(0) as u32;
        output.push(TABLE[((first >> 2) & 63) as usize] as char);
        output.push(TABLE[((first << 4 | second >> 4) & 63) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[((second << 2 | third >> 6) & 63) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(third & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}

fn mode(metadata: &std::fs::Metadata) -> Option<u32> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        Some(metadata.permissions().mode())
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        None
    }
}

fn modified_millis(metadata: &std::fs::Metadata) -> Option<u64> {
    metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_millis() as u64)
}

/// Exact recursive directory payload shared by all mutation owners. The
/// payload is opaque to the history worker but content-bound by its digest.
pub(crate) fn directory_manifest(path: &Path) -> Result<Vec<u8>, String> {
    let root = fs::canonicalize(path).map_err(|error| error.to_string())?;
    let mut entries = Vec::new();
    let mut stack = vec![root.clone()];
    while let Some(current) = stack.pop() {
        let mut children = fs::read_dir(&current)
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        children.sort_by_key(|entry| entry.file_name());
        for entry in children.into_iter().rev() {
            let path = entry.path();
            let relative = path
                .strip_prefix(&root)
                .map_err(|error| error.to_string())?
                .to_string_lossy()
                .replace('\\', "/");
            let metadata = fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
            let mut record = json!({
                "path": relative,
                "mode": mode(&metadata),
                "mtime_millis": modified_millis(&metadata),
            });
            if metadata.file_type().is_symlink() {
                record["type"] = json!("symlink");
                record["target"] = json!(fs::read_link(&path).map_err(|error| error.to_string())?.to_string_lossy());
            } else if metadata.is_dir() {
                record["type"] = json!("directory");
                stack.push(path);
            } else {
                let bytes = fs::read(&path).map_err(|error| error.to_string())?;
                record["type"] = json!("file");
                record["size"] = json!(bytes.len());
                record["sha256"] = json!(format!("sha256:{}", encode_hex(&Sha256::digest(&bytes))));
                record["content"] = json!(encode_base64(&bytes));
            }
            entries.push(record);
        }
    }
    serde_json::to_vec(&json!({"version": 1, "root_type": "directory", "entries": entries}))
        .map_err(|error| error.to_string())
}

fn encode_hex(bytes: &[u8]) -> String {
    const TABLE: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(TABLE[(byte >> 4) as usize] as char);
        output.push(TABLE[(byte & 0x0f) as usize] as char);
    }
    output
}


#[cfg(windows)]
mod windows_channel {
    use super::*;
    use std::{ffi::c_void, io, time::{Duration, Instant}};
    type Handle = *mut c_void;
    #[repr(C)]
    struct Overlapped { internal: usize, internal_high: usize, offset: u32, offset_high: u32, event: Handle }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateFileW(name: *const u16, access: u32, share: u32, attrs: *mut c_void, disposition: u32, flags: u32, template: Handle) -> Handle;
        fn WaitNamedPipeW(name: *const u16, timeout: u32) -> i32;
        fn CreateEventW(attrs: *mut c_void, manual: i32, initial: i32, name: *const u16) -> Handle;
        fn ResetEvent(event: Handle) -> i32;
        fn CloseHandle(handle: Handle) -> i32;
        fn ReadFile(handle: Handle, buffer: *mut c_void, size: u32, read: *mut u32, ov: *mut Overlapped) -> i32;
        fn WriteFile(handle: Handle, buffer: *const c_void, size: u32, written: *mut u32, ov: *mut Overlapped) -> i32;
        fn WaitForSingleObject(handle: Handle, timeout: u32) -> u32;
        fn GetOverlappedResult(handle: Handle, ov: *mut Overlapped, count: *mut u32, wait: i32) -> i32;
        fn CancelIoEx(handle: Handle, ov: *mut Overlapped) -> i32;
    }
    struct Owned(Handle);
    impl Drop for Owned { fn drop(&mut self) { unsafe { CloseHandle(self.0); } } }
    pub fn exchange(path: &Path, frame: &[u8]) -> io::Result<Vec<u8>> {
        let logical = path.to_str().ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "History endpoint is not Unicode"))?;
        let name = format!(r"\\.\pipe\openclank-history-{:x}", Sha256::digest(logical.as_bytes()));
        let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        let deadline = Instant::now() + Duration::from_secs(10);
        let remaining = || deadline.saturating_duration_since(Instant::now()).as_millis().min(u32::MAX as u128) as u32;
        let pipe = loop {
            let handle = unsafe { CreateFileW(wide.as_ptr(), 0xc0000000, 0, std::ptr::null_mut(), 3, 0x40000000, std::ptr::null_mut()) };
            if handle as isize != -1 { break Owned(handle); }
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(231) || remaining() == 0 { return Err(error); }
            if unsafe { WaitNamedPipeW(wide.as_ptr(), remaining()) } == 0 { return Err(io::Error::last_os_error()); }
        };
        let event = Owned(unsafe { CreateEventW(std::ptr::null_mut(), 1, 0, std::ptr::null()) });
        if event.0.is_null() { return Err(io::Error::last_os_error()); }
        let transfer = |buffer: *mut c_void, size: usize, write: bool| -> io::Result<usize> {
            unsafe { ResetEvent(event.0); }
            let mut ov = Overlapped { internal: 0, internal_high: 0, offset: 0, offset_high: 0, event: event.0 };
            let mut count = 0;
            let ok = unsafe { if write { WriteFile(pipe.0, buffer, size as u32, &mut count, &mut ov) } else { ReadFile(pipe.0, buffer, size as u32, &mut count, &mut ov) } };
            if ok == 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(997) { return Err(error); }
                if unsafe { WaitForSingleObject(event.0, remaining()) } != 0 {
                    unsafe { CancelIoEx(pipe.0, &mut ov); GetOverlappedResult(pipe.0, &mut ov, &mut count, 1); }
                    return Err(io::Error::new(io::ErrorKind::TimedOut, "History pipe deadline expired"));
                }
                if unsafe { GetOverlappedResult(pipe.0, &mut ov, &mut count, 0) } == 0 { return Err(io::Error::last_os_error()); }
            }
            Ok(count as usize)
        };
        let mut sent = 0;
        while sent < frame.len() {
            let count = transfer(frame[sent..].as_ptr() as *mut c_void, frame.len() - sent, true)?;
            if count == 0 { return Err(io::Error::new(io::ErrorKind::WriteZero, "History pipe write made no progress")); }
            sent += count;
        }
        let mut response = Vec::new();
        loop {
            let mut chunk = [0u8; 65536];
            let count = transfer(chunk.as_mut_ptr() as *mut c_void, chunk.len(), false)?;
            if count == 0 { return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "History response ended before newline")); }
            response.extend_from_slice(&chunk[..count]);
            if response.len() > 1024 * 1024 { return Err(io::Error::new(io::ErrorKind::InvalidData, "History response exceeds frame limit")); }
            if response.last() == Some(&b'\n') { return Ok(response); }
        }
    }
}
