use super::MemeryWorker;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{self, ErrorKind};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinSet;

const VERSION: u8 = 1;
const MAX_FRAME: usize = 32 * 1024 * 1024;
const MAX_ACTIVE: usize = 16;

#[derive(Debug)]
pub struct WorkerError {
    pub code: &'static str,
    message: String,
    pub data: Option<Value>,
}

impl WorkerError {
    pub fn invalid_params(message: impl ToString, data: Option<Value>) -> Self {
        Self { code: "invalid", message: message.to_string(), data }
    }

    pub fn internal_error(message: impl ToString, data: Option<Value>) -> Self {
        Self { code: "internal", message: message.to_string(), data }
    }

    fn status(code: &'static str) -> Self {
        Self { code, message: code.to_owned(), data: None }
    }

    fn reply(&self, id: &str) -> Value {
        let code = self.data.as_ref().and_then(|data| data["code"].as_str()).unwrap_or(self.code);
        json!({"version": VERSION, "type":"reply", "id":id,
               "error":{"code":code, "message":self.message, "data":self.data}})
    }
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Call { version: u8, id: String, operation: String, arguments: Value, deadline_ms: u64 },
    Cancel { version: u8, id: String },
}

async fn read_frames(sender: mpsc::Sender<Request>) -> io::Result<()> {
    let mut input = tokio::io::stdin();
    loop {
        let size = input.read_u32().await? as usize;
        if size == 0 || size > MAX_FRAME {
            return Err(io::Error::new(ErrorKind::InvalidData, "invalid Memery frame size"));
        }
        let mut bytes = vec![0; size];
        input.read_exact(&mut bytes).await?;
        let request: Request = serde_json::from_slice(&bytes)?;
        let (version, id) = match &request {
            Request::Call { version, id, .. } | Request::Cancel { version, id } => (*version, id),
        };
        if version != VERSION || id.is_empty() || id.len() > 128 {
            return Err(io::Error::new(ErrorKind::InvalidData, "invalid Memery version or request id"));
        }
        if sender.send(request).await.is_err() { return Ok(()); }
    }
}

async fn write_frame(out: &mut tokio::io::Stdout, value: &Value) -> io::Result<()> {
    let mut bytes = serde_json::to_vec(value)?;
    if bytes.len() > MAX_FRAME {
        let id = value["id"].as_str().ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "oversized handshake"))?;
        bytes = serde_json::to_vec(&WorkerError::internal_error("Memery response exceeds the frame limit", None).reply(id))?;
    }
    out.write_u32(bytes.len() as u32).await?;
    out.write_all(&bytes).await?;
    out.flush().await
}

fn interruptible(operation: &str, arguments: &Value) -> bool {
    if operation == "memory_quality" { return arguments["rebuild_graph_fts"] != true; }
    matches!(operation, "digest" | "search" | "recall" | "get_memory" | "list_memories"
        | "memory_explain" | "memory_export" | "list_quarantine"
        | "list_candidates" | "graph_walk")
}

pub async fn serve(worker: MemeryWorker) -> io::Result<()> {
    let mut output = tokio::io::stdout();
    write_frame(&mut output, &json!({"version":VERSION, "type":"ready", "profile":worker.profile_receipt})).await?;
    // The reader owns partial frames across dispatch and reply wakeups.
    let (sender, mut requests) = mpsc::channel(1);
    let reader = tokio::spawn(read_frames(sender));
    let mut active: HashMap<String, Option<oneshot::Sender<()>>> = HashMap::new();
    let mut jobs = JoinSet::<(String, Value)>::new();
    loop {
        tokio::select! {
            completed = jobs.join_next(), if !jobs.is_empty() => {
                let (id, reply) = completed.expect("active job")
                    .map_err(|_| io::Error::other("Memery operation task failed"))?;
                active.remove(&id);
                write_frame(&mut output, &reply).await?;
            }
            request = requests.recv() => match request {
                None => break,
                Some(Request::Cancel { id, .. }) => {
                    if let Some(Some(signal)) = active.get_mut(&id).map(Option::take) {
                        let _ = signal.send(());
                    }
                }
                Some(Request::Call { id, operation, arguments, deadline_ms, .. }) => {
                    if active.contains_key(&id) {
                        return Err(io::Error::new(ErrorKind::InvalidData, "duplicate outstanding Memery id"));
                    }
                    if active.len() >= MAX_ACTIVE {
                        write_frame(&mut output, &WorkerError::status("busy").reply(&id)).await?;
                        continue;
                    }
                    let worker = worker.clone();
                    let (signal, cancel) = oneshot::channel();
                    active.insert(id.clone(), Some(signal));
                    // Repository calls may synchronously walk files or rebuild SQLite indexes.
                    // Give each bounded operation a blocking thread while the async runtime
                    // remains available for pipe I/O, recall dispatch and cancellation.
                    let runtime = tokio::runtime::Handle::current();
                    jobs.spawn_blocking(move || runtime.block_on(async move {
                        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
                        let remaining = (deadline_ms as u128).saturating_sub(now) as u64;
                        let result = if remaining == 0 {
                            Err(WorkerError::status("deadline"))
                        } else if !arguments.is_object() {
                            Err(WorkerError::invalid_params("expected operation arguments object", None))
                        } else if interruptible(&operation, &arguments) {
                            tokio::select! {
                                biased;
                                _ = cancel => Err(WorkerError::status("cancelled")),
                                _ = tokio::time::sleep(Duration::from_millis(remaining)) => Err(WorkerError::status("deadline")),
                                result = worker.dispatch(&operation, arguments) => result,
                            }
                        } else {
                            // An admitted mutation must reach its own transaction/receipt boundary.
                            worker.dispatch(&operation, arguments).await
                        };
                        let reply = match result {
                            Ok(value) => json!({"version":VERSION,"type":"reply","id":id,"result":value}),
                            Err(error) => error.reply(&id),
                        };
                        (id, reply)
                    }));
                }
            }
        }
    }
    match reader.await.map_err(|_| io::Error::other("Memery input task failed"))? {
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => Ok(()),
        other => other,
    }
}
