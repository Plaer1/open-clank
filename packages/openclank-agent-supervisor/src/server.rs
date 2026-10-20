use crate::runtime_actor::{OwnerRuntime, OwnerRuntimeActor, OwnerRuntimeHandle};
use crate::{build_id, schema_sha256, Readiness, PROTOCOL_MAJOR};
use base64::Engine;
use protocol::{
    agent_supervisor_server::AgentSupervisor, AckSemanticRequest, AckSemanticResponse,
    ActivateDriverCallbackRequest, ActivateDriverCallbackResponse, Capability, HealthRequest,
    CloseSessionRequest, CloseSessionResponse, HealthResponse, NegotiateRequest, NegotiateResponse,
    OpenOwnerRuntimeRequest,
    OpenOwnerRuntimeResponse, OpenSessionRequest, OpenSessionResponse, OwnerRef, ProtocolVersion,
    RequestMeta, RuntimeBinding, SemanticEvent as WireSemanticEvent, SemanticStreamItem,
    SessionBinding, SupervisorError, TerminalDescriptor, TurnRequest, TurnStreamItem,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use tonic::metadata::MetadataValue;
use tonic::{Request, Response, Status};

use crate::protocol;
use crate::semantic::{SemanticEpoch, SemanticEvent, SemanticJournal};

struct RuntimeEntry {
    _actor: OwnerRuntimeActor,
    handle: OwnerRuntimeHandle,
    binding: RuntimeBinding,
    driver_pid: u64,
    driver_start_token: String,
    callback_registration_id: String,
}

struct SessionEntry {
    binding: SessionBinding,
    semantic_epoch: Vec<u8>,
    journal: SemanticJournal,
}

#[derive(Debug, Clone)]
pub struct DriverLaunchSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
}

#[derive(Clone)]
pub struct HealthOnlyService {
    runtimes: Arc<Mutex<BTreeMap<String, RuntimeEntry>>>,
    sessions: Arc<Mutex<BTreeMap<String, SessionEntry>>>,
    driver_launch: Option<Arc<DriverLaunchSpec>>,
}

impl Default for HealthOnlyService {
    fn default() -> Self {
        Self {
            runtimes: Arc::new(Mutex::new(BTreeMap::new())),
            sessions: Arc::new(Mutex::new(BTreeMap::new())),
            driver_launch: None,
        }
    }
}

impl HealthOnlyService {
    /// Opt-in launch configuration. The RPC never accepts an executable or
    /// shell fragment from its caller; the daemon owner supplies this once at
    /// startup and SpawnSpec still applies the absolute-cwd/minimal-env
    /// boundary.
    pub fn with_driver_launch(spec: DriverLaunchSpec) -> Self {
        Self {
            runtimes: Arc::new(Mutex::new(BTreeMap::new())),
            sessions: Arc::new(Mutex::new(BTreeMap::new())),
            driver_launch: Some(Arc::new(spec)),
        }
    }
}

fn safe_error(
    request_id: impl Into<String>,
    code: &str,
    message: &str,
    phase: &str,
    retryable: bool,
) -> SupervisorError {
    SupervisorError {
        code: code.to_owned(),
        safe_message: message.to_owned(),
        retryable,
        phase: phase.to_owned(),
        request_id: request_id.into(),
    }
}

fn hex_bytes(bytes: &[u8]) -> String {
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write;
        let _ = write!(result, "{byte:02x}");
    }
    result
}

fn decode_hex(value: &str) -> Vec<u8> {
    let bytes = value.as_bytes();
    if bytes.len() % 2 != 0 {
        return Vec::new();
    }
    bytes
        .chunks_exact(2)
        .map(|chunk| {
            let high = (chunk[0] as char).to_digit(16).unwrap_or(0);
            let low = (chunk[1] as char).to_digit(16).unwrap_or(0);
            ((high << 4) | low) as u8
        })
        .collect()
}

fn request_meta(meta: Option<&RequestMeta>) -> Result<(&OwnerRef, String), SupervisorError> {
    let meta = meta.ok_or_else(|| {
        safe_error(
            "",
            "malformed_request",
            "request metadata is required",
            "protocol",
            false,
        )
    })?;
    let request_id = meta.request_id.clone();
    let owner = meta.owner.as_ref().ok_or_else(|| {
        safe_error(
            &request_id,
            "malformed_request",
            "owner binding is required",
            "authority",
            false,
        )
    })?;
    if owner.subject_id.is_empty() || owner.subject_id.len() > 128 {
        return Err(safe_error(
            &request_id,
            "malformed_request",
            "owner binding is malformed",
            "authority",
            false,
        ));
    }
    let Some(protocol) = meta.protocol.as_ref() else {
        return Err(safe_error(
            &request_id,
            "malformed_request",
            "negotiated protocol binding is required",
            "protocol",
            false,
        ));
    };
    if protocol.major > 1 || protocol.minor > 0 {
        return Err(safe_error(
            &request_id,
            "incompatible_protocol",
            "supervisor protocol is not supported",
            "protocol",
            false,
        ));
    }
    if protocol.schema_sha256 != schema_sha256().as_bytes() {
        return Err(safe_error(
            &request_id,
            "schema_mismatch",
            "supervisor schema identity is not supported",
            "protocol",
            false,
        ));
    }
    Ok((owner, request_id))
}

fn owner_binding_matches(bound: Option<&OwnerRef>, requested: &OwnerRef) -> bool {
    bound.map(|value| {
        value.subject_id == requested.subject_id
            && value.auth_generation == requested.auth_generation
    }) == Some(true)
}

fn runtime_capabilities() -> Vec<Capability> {
    vec![
        Capability {
            name: "runtime_actor".into(),
            supported: true,
            detail_code: "generation_fenced".into(),
        },
        Capability {
            name: "driver".into(),
            supported: false,
            detail_code: "driver_not_attached".into(),
        },
    ]
}

fn attached_runtime_capabilities() -> Vec<Capability> {
    let mut capabilities = runtime_capabilities();
    if let Some(driver) = capabilities
        .iter_mut()
        .find(|capability| capability.name == "driver")
    {
        driver.supported = true;
        driver.detail_code = "driver_attached_waiting_activation".into();
    }
    capabilities
}

fn driver_capabilities(payload: Option<&serde_json::Value>) -> Vec<Capability> {
    payload
        .and_then(serde_json::Value::as_object)
        .and_then(|value| value.get("capabilities"))
        .and_then(serde_json::Value::as_object)
        .map(|capabilities| {
            capabilities
                .iter()
                .filter_map(|(name, value)| {
                    if name.is_empty() || name.len() > 64 {
                        return None;
                    }
                    value.as_bool().map(|supported| Capability {
                        name: name.clone(),
                        supported,
                        detail_code: if supported {
                            "driver_declared"
                        } else {
                            "driver_unavailable"
                        }
                        .into(),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn runtime_key(owner: &OwnerRef, adapter_id: &str, generation: u64, fingerprint: &[u8]) -> String {
    format!(
        "{}\u{1f}{}\u{1f}{}\u{1f}{}\u{1f}{}",
        owner.subject_id,
        owner.auth_generation,
        adapter_id,
        generation,
        hex_bytes(fingerprint)
    )
}

fn runtime_binding(
    owner: &OwnerRef,
    adapter_id: &str,
    generation: u64,
    fingerprint: &[u8],
    key: &str,
) -> RuntimeBinding {
    let mut digest = Sha256::new();
    digest.update(key.as_bytes());
    let digest = digest.finalize();
    RuntimeBinding {
        owner: Some(owner.clone()),
        runtime_id: format!("rt-{}", hex_bytes(&digest[..16])),
        epoch: digest[..16].to_vec(),
        generation,
        fingerprint: fingerprint.to_vec(),
        adapter_id: adapter_id.to_owned(),
    }
}

fn driver_events(
    payload: Option<&serde_json::Value>,
    owner: &OwnerRef,
    session: &SessionBinding,
    semantic_epoch: &str,
    run_id: &str,
    turn_id: &str,
) -> Result<Vec<SemanticEvent>, &'static str> {
    let Some(events) = payload
        .and_then(serde_json::Value::as_object)
        .and_then(|value| value.get("events"))
        .and_then(serde_json::Value::as_array)
    else {
        return Err("driver returned no semantic event batch");
    };
    if events.is_empty() || events.len() > 64 {
        return Err("driver semantic event batch is outside the bounded limit");
    }
    let runtime = session
        .runtime
        .as_ref()
        .ok_or("session runtime is missing")?;
    let runtime_epoch = hex_bytes(&runtime.epoch);
    let semantic_epoch = semantic_epoch.to_owned();
    events
        .iter()
        .map(|raw| {
            let object = raw
                .as_object()
                .ok_or("driver semantic event must be an object")?;
            let bounded_text = |name: &str, limit: usize| {
                object
                    .get(name)
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| value.len() <= limit)
                    .ok_or("driver semantic event identity is malformed")
            };
            let op = bounded_text("op", 64)?.to_owned();
            if !matches!(
                op.as_str(),
                "snapshot_reset"
                    | "turn_upsert"
                    | "turn_remove"
                    | "message_upsert"
                    | "thought_upsert"
                    | "text_append"
                    | "tool_upsert"
                    | "tool_progress"
                    | "tool_result"
                    | "plan_upsert"
                    | "task_upsert"
                    | "interaction_upsert"
                    | "interaction_remove"
                    | "secret_request"
                    | "secret_resolved"
                    | "usage_merge"
                    | "metrics_merge"
                    | "commands_replace"
                    | "config_merge"
                    | "actor_upsert"
                    | "actor_remove"
                    | "session_merge"
                    | "error_upsert"
                    | "terminal_upsert"
                    | "terminal_remove"
                    | "run_completed"
                    | "run_cancelled"
                    | "resync_required"
            ) {
                return Err("driver semantic event operation is not supported");
            }
            let entity_kind = bounded_text("entity_kind", 64)?.to_owned();
            let entity_id = bounded_text("entity_id", 128)?.to_owned();
            let event_payload = object.get("payload").cloned().unwrap_or_else(|| json!({}));
            if !event_payload.is_object() {
                return Err("driver semantic event payload must be an object");
            }
            let append_offset_utf8_bytes = object
                .get("append_offset_utf8_bytes")
                .map(|value| value.as_u64().ok_or("driver append offset is malformed"))
                .transpose()?;
            if op == "text_append" && append_offset_utf8_bytes.is_none() {
                return Err("driver text append requires a UTF-8 offset");
            }
            if op != "text_append" && append_offset_utf8_bytes.is_some() {
                return Err("driver append offset is only valid for text append");
            }
            Ok(SemanticEvent {
                owner_subject_id: owner.subject_id.clone(),
                session_id: session.session_id.clone(),
                runtime_id: runtime.runtime_id.clone(),
                runtime_epoch: runtime_epoch.clone(),
                runtime_generation: runtime.generation,
                run_id: run_id.to_owned(),
                turn_id: turn_id.to_owned(),
                semantic_epoch: semantic_epoch.clone(),
                seq: 0,
                emitted_unix_ms: 0,
                op,
                entity_kind,
                entity_id,
                payload: event_payload,
                payload_sha256: String::new(),
                append_offset_utf8_bytes,
            })
        })
        .collect()
}

fn semantic_event_for_grade(event: SemanticEvent, grade: &str) -> Option<SemanticEvent> {
    if grade == "off" {
        return None;
    }
    let retained = match grade {
        "delta" => true,
        // Block delivery is the browser-safe default: complete upserts and
        // merges are retained, while token append deltas are represented by a
        // same-sequence placeholder so cursors remain contiguous.
        "block" => event.op != "text_append",
        // Turn delivery exposes only turn lifecycle/snapshot state. The
        // snapshot is retained so a fresh reducer can initialize safely.
        "turn" => matches!(
            event.op.as_str(),
            "snapshot_reset" | "turn_upsert" | "turn_remove" | "run_completed" | "run_cancelled"
        ),
        _ => false,
    };
    if retained {
        return Some(event);
    }
    let mut filtered = event;
    filtered.op = "filtered".into();
    filtered.entity_kind.clear();
    filtered.entity_id.clear();
    filtered.payload = json!({});
    filtered.payload_sha256 = format!("{:x}", Sha256::digest(b"{}"));
    filtered.append_offset_utf8_bytes = None;
    Some(filtered)
}

#[tonic::async_trait]
impl AgentSupervisor for HealthOnlyService {
    type AttachSemanticStream = Pin<
        Box<dyn tokio_stream::Stream<Item = Result<SemanticStreamItem, Status>> + Send + 'static>,
    >;
    type RunTurnStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<TurnStreamItem, Status>> + Send + 'static>>;

    async fn negotiate(
        &self,
        request: Request<NegotiateRequest>,
    ) -> Result<Response<NegotiateResponse>, Status> {
        let request = request.into_inner();
        let request_id = request.request_id;
        let protocol = request.protocol.unwrap_or_default();
        let server_protocol = ProtocolVersion {
            major: 1,
            minor: 0,
            schema_sha256: schema_sha256().into_bytes(),
            features: vec!["transport".into(), "protocol".into(), "health".into()],
        };
        let capabilities = vec![
            Capability {
                name: "transport".into(),
                supported: true,
                detail_code: "unix_uds_peer_uid".into(),
            },
            Capability {
                name: "protocol".into(),
                supported: true,
                detail_code: "v1".into(),
            },
            Capability {
                name: "health".into(),
                supported: true,
                detail_code: "phased".into(),
            },
            Capability {
                name: "runtime_actor".into(),
                supported: true,
                detail_code: "generation_fenced_rpc".into(),
            },
            Capability {
                name: "session".into(),
                supported: true,
                detail_code: "bounded_snapshot_epoch".into(),
            },
            Capability {
                name: "driver".into(),
                supported: self.driver_launch.is_some(),
                detail_code: if self.driver_launch.is_some() {
                    "direct_driver_lifecycle"
                } else {
                    "launch_not_configured"
                }
                .into(),
            },
        ];
        let mut supported = vec![
            "transport",
            "protocol",
            "health",
            "runtime_actor",
            "session",
        ];
        if self.driver_launch.is_some() {
            supported.push("driver");
        }
        let error = if protocol.major != 1 || protocol.minor > 0 {
            Some(SupervisorError {
                code: "incompatible_protocol".into(),
                safe_message: "supervisor protocol is not supported".into(),
                retryable: false,
                phase: "protocol".into(),
                request_id: request_id.clone(),
            })
        } else if protocol.schema_sha256 != schema_sha256().as_bytes()
            && !protocol.schema_sha256.is_empty()
        {
            Some(SupervisorError {
                code: "schema_mismatch".into(),
                safe_message: "supervisor schema identity is not supported".into(),
                retryable: false,
                phase: "protocol".into(),
                request_id: request_id.clone(),
            })
        } else if request
            .required_features
            .iter()
            .any(|feature| !supported.contains(&feature.as_str()))
        {
            Some(SupervisorError {
                code: "feature_missing".into(),
                safe_message: "one or more required supervisor features are unavailable".into(),
                retryable: false,
                phase: "protocol".into(),
                request_id: request_id.clone(),
            })
        } else {
            None
        };
        Ok(Response::new(NegotiateResponse {
            protocol: Some(server_protocol),
            server_build: build_id().to_owned(),
            capabilities,
            error,
        }))
    }

    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let readiness = Readiness::health_only();
        Ok(Response::new(HealthResponse {
            protocol_major: PROTOCOL_MAJOR.to_owned(),
            build_id: build_id().to_owned(),
            transport: readiness.transport,
            protocol: readiness.protocol,
            containment: readiness.containment,
            runtime_actor: readiness.runtime_actor,
            driver: readiness.driver,
            tool_policy: readiness.tool_policy,
            ready: readiness.ready,
            schema_sha256: schema_sha256(),
        }))
    }

    async fn open_owner_runtime(
        &self,
        request: Request<OpenOwnerRuntimeRequest>,
    ) -> Result<Response<OpenOwnerRuntimeResponse>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                return Ok(Response::new(OpenOwnerRuntimeResponse {
                    capabilities: runtime_capabilities(),
                    error: Some(error),
                    ..Default::default()
                }));
            }
        };
        if request.adapter_id.is_empty() || request.adapter_id.len() > 128 {
            return Ok(Response::new(OpenOwnerRuntimeResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "adapter identity is malformed",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        if request.projection_fingerprint.len() != 32 {
            return Ok(Response::new(OpenOwnerRuntimeResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "projection fingerprint must be 32 bytes",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        let key = runtime_key(
            owner,
            &request.adapter_id,
            request.expected_generation,
            &request.projection_fingerprint,
        );
        let mut runtimes = self
            .runtimes
            .lock()
            .map_err(|_| Status::internal("runtime registry unavailable"))?;
        if let Some(entry) = runtimes.get(&key) {
            return Ok(Response::new(OpenOwnerRuntimeResponse {
                runtime: Some(entry.binding.clone()),
                capabilities: if entry.driver_pid == 0 {
                    runtime_capabilities()
                } else {
                    attached_runtime_capabilities()
                },
                driver_pid: entry.driver_pid,
                driver_start_token: entry.driver_start_token.clone(),
                callback_registration_id: entry.callback_registration_id.clone(),
                driver_ready: false,
                ..Default::default()
            }));
        }
        let runtime = OwnerRuntime::new(
            owner.subject_id.clone(),
            request.expected_generation,
            hex_bytes(&request.projection_fingerprint),
        );
        let actor = OwnerRuntimeActor::spawn(runtime, 32)
            .map_err(|_| Status::resource_exhausted("owner runtime command queue unavailable"))?;
        let handle = actor.handle();
        let binding = runtime_binding(
            owner,
            &request.adapter_id,
            request.expected_generation,
            &request.projection_fingerprint,
            &key,
        );
        let mut driver_pid = 0_u64;
        let mut driver_start_token = String::new();
        let mut callback_registration_id = String::new();
        if let Some(spec) = self.driver_launch.as_deref() {
            #[cfg(unix)]
            {
                let launch = crate::process::SpawnSpec {
                    program: spec.program.clone(),
                    args: spec.args.clone(),
                    cwd: spec.cwd.clone(),
                    environment: BTreeMap::new(),
                };
                let runtime_epoch = hex_bytes(&binding.epoch);
                let mut binding_digest = Sha256::new();
                binding_digest.update(binding.runtime_id.as_bytes());
                binding_digest.update(request_id.as_bytes());
                let binding_sha256 = hex_bytes(&binding_digest.finalize());
                let process = match crate::driver_process::DirectDriverProcess::spawn(
                    &launch,
                    &[],
                    crate::driver_process::DriverIdentity {
                        owner_subject_id: owner.subject_id.clone(),
                        session_id: "owner-runtime".into(),
                        runtime_id: binding.runtime_id.clone(),
                        runtime_epoch,
                        runtime_generation: binding.generation,
                    },
                    schema_sha256(),
                    binding_sha256,
                ) {
                    Ok(process) => process,
                    Err(_) => {
                        return Ok(Response::new(OpenOwnerRuntimeResponse {
                            capabilities: runtime_capabilities(),
                            error: Some(safe_error(
                                request_id,
                                "unavailable",
                                "driver process could not be admitted",
                                "driver",
                                true,
                            )),
                            ..Default::default()
                        }));
                    }
                };
                driver_pid = process.pid() as u64;
                driver_start_token = match process.start_token() {
                    Ok(token) => token,
                    Err(_) => {
                        return Ok(Response::new(OpenOwnerRuntimeResponse {
                            capabilities: runtime_capabilities(),
                            error: Some(safe_error(
                                request_id,
                                "unavailable",
                                "driver process identity is unavailable",
                                "driver",
                                true,
                            )),
                            ..Default::default()
                        }));
                    }
                };
                let mut registration_digest = Sha256::new();
                registration_digest.update(binding.runtime_id.as_bytes());
                registration_digest.update(b"callback");
                callback_registration_id = hex_bytes(&registration_digest.finalize()[..16]);
                if handle.attach_driver(process).is_err() {
                    return Ok(Response::new(OpenOwnerRuntimeResponse {
                        capabilities: runtime_capabilities(),
                        error: Some(safe_error(
                            request_id,
                            "unavailable",
                            "driver process could not be attached",
                            "runtime_actor",
                            true,
                        )),
                        ..Default::default()
                    }));
                }
            }
            #[cfg(not(unix))]
            {
                return Ok(Response::new(OpenOwnerRuntimeResponse {
                    capabilities: runtime_capabilities(),
                    error: Some(safe_error(
                        request_id,
                        "unavailable",
                        "driver process backend is unavailable",
                        "containment",
                        true,
                    )),
                    ..Default::default()
                }));
            }
        }
        runtimes.insert(
            key,
            RuntimeEntry {
                _actor: actor,
                handle,
                binding: binding.clone(),
                driver_pid,
                driver_start_token: driver_start_token.clone(),
                callback_registration_id: callback_registration_id.clone(),
            },
        );
        Ok(Response::new(OpenOwnerRuntimeResponse {
            runtime: Some(binding),
            capabilities: if driver_pid == 0 {
                runtime_capabilities()
            } else {
                attached_runtime_capabilities()
            },
            driver_pid,
            driver_start_token,
            callback_registration_id,
            driver_ready: false,
            ..Default::default()
        }))
    }

    async fn open_session(
        &self,
        request: Request<OpenSessionRequest>,
    ) -> Result<Response<OpenSessionResponse>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                return Ok(Response::new(OpenSessionResponse {
                    error: Some(error),
                    ..Default::default()
                }));
            }
        };
        let runtime = match request.runtime.as_ref() {
            Some(binding) => binding,
            None => {
                return Ok(Response::new(OpenSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "malformed_request",
                        "runtime binding is required",
                        "runtime_actor",
                        false,
                    )),
                    ..Default::default()
                }));
            }
        };
        if request.session_id.is_empty()
            || request.session_id.len() > 128
            || request.workspace_id.len() > 128
        {
            return Ok(Response::new(OpenSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "session binding is malformed",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        if !owner_binding_matches(runtime.owner.as_ref(), owner) {
            return Ok(Response::new(OpenSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "forbidden",
                    "session owner binding is not permitted",
                    "authority",
                    false,
                )),
                ..Default::default()
            }));
        }
        if request.snapshot_json.len() > 1024 * 1024 || request.config_json.len() > 1024 * 1024 {
            return Ok(Response::new(OpenSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "session snapshot exceeds the bounded limit",
                    "protocol",
                    false,
                )),
                ..Default::default()
            }));
        }
        if !request.snapshot_sha256.is_empty() {
            if request.snapshot_sha256.len() != 32
                || Sha256::digest(&request.snapshot_json).as_slice() != request.snapshot_sha256
            {
                return Ok(Response::new(OpenSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "malformed_request",
                        "session snapshot digest does not match",
                        "protocol",
                        false,
                    )),
                    ..Default::default()
                }));
            }
        }
        let snapshot_payload = if request.snapshot_json.is_empty() {
            None
        } else {
            let payload = match serde_json::from_slice::<serde_json::Value>(&request.snapshot_json)
            {
                Ok(payload) if payload.is_object() => payload,
                _ => {
                    return Ok(Response::new(OpenSessionResponse {
                        error: Some(safe_error(
                            request_id,
                            "malformed_request",
                            "session snapshot must be a JSON object",
                            "protocol",
                            false,
                        )),
                        ..Default::default()
                    }));
                }
            };
            Some(payload)
        };
        let runtimes = self
            .runtimes
            .lock()
            .map_err(|_| Status::internal("runtime registry unavailable"))?;
        let Some(runtime_entry) = runtimes
            .values()
            .find(|entry| entry.binding.runtime_id == runtime.runtime_id)
        else {
            return Ok(Response::new(OpenSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_generation",
                    "runtime binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        };
        if runtime_entry.binding != *runtime {
            return Ok(Response::new(OpenSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_generation",
                    "runtime binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        let runtime_handle = runtime_entry.handle.clone();
        drop(runtimes);

        let session_key = format!("{}\u{1f}{}", runtime.runtime_id, request.session_id);
        let mut sessions = self
            .sessions
            .lock()
            .map_err(|_| Status::internal("session registry unavailable"))?;
        if let Some(entry) = sessions.get(&session_key) {
            if entry.binding.workspace_id != request.workspace_id {
                return Ok(Response::new(OpenSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "stale_session",
                        "session workspace binding is not current",
                        "runtime_actor",
                        false,
                    )),
                    ..Default::default()
                }));
            }
            let existing_binding = entry.binding.clone();
            let existing_epoch = entry.semantic_epoch.clone();
            let existing_head = entry
                .journal
                .tail_seq()
                .saturating_sub(entry.journal.retained_events().saturating_sub(1) as u64);
            let existing_tail = entry.journal.tail_seq();
            drop(sessions);
            let restore = !request.snapshot_json.is_empty()
                || request.transcript_revision > 0
                || !request.config_json.is_empty();
            let driver_result = if restore {
                runtime_handle.restore_session(
                    existing_binding.session_id.clone(),
                    json!({
                        "workspace_id": existing_binding.workspace_id,
                        "snapshot_sha256": hex_bytes(&Sha256::digest(&request.snapshot_json)),
                        "snapshot_bytes": request.snapshot_json.len(),
                        "transcript_revision": request.transcript_revision,
                        "config_sha256": hex_bytes(&Sha256::digest(&request.config_json)),
                        "config_bytes": request.config_json.len(),
                    }),
                )
            } else {
                runtime_handle.open_session(
                    existing_binding.session_id.clone(),
                    json!({
                        "workspace_id": existing_binding.workspace_id,
                        "snapshot_sha256": hex_bytes(&Sha256::digest(&request.snapshot_json)),
                        "snapshot_bytes": request.snapshot_json.len(),
                        "transcript_revision": request.transcript_revision,
                        "config_sha256": hex_bytes(&Sha256::digest(&request.config_json)),
                        "config_bytes": request.config_json.len(),
                    }),
                )
            };
            if let Err(error) = driver_result {
                if !matches!(error, crate::runtime_actor::ActorError::DriverNotAttached) {
                    return Ok(Response::new(OpenSessionResponse {
                        error: Some(safe_error(
                            request_id,
                            "driver_unavailable",
                            "driver session could not be reopened",
                            "driver",
                            true,
                        )),
                        ..Default::default()
                    }));
                }
            }
            return Ok(Response::new(OpenSessionResponse {
                session: Some(existing_binding),
                semantic_epoch: existing_epoch,
                retained_head: existing_head,
                retained_tail: existing_tail,
                terminals: Vec::new(),
                ..Default::default()
            }));
        }
        let mut epoch_digest = Sha256::new();
        epoch_digest.update(runtime.runtime_id.as_bytes());
        epoch_digest.update(request.session_id.as_bytes());
        epoch_digest.update(request.transcript_revision.to_be_bytes());
        let epoch_digest = epoch_digest.finalize();
        let epoch = epoch_digest[..16].to_vec();
        let mut epoch_array = [0_u8; 16];
        epoch_array.copy_from_slice(&epoch);
        let binding = SessionBinding {
            runtime: Some(runtime.clone()),
            session_id: request.session_id,
            workspace_id: request.workspace_id,
        };
        let mut journal = SemanticJournal::new(SemanticEpoch(epoch_array), 16 * 1024 * 1024, 2_000);
        if let Some(payload) = snapshot_payload {
            let _ = journal.append(SemanticEvent {
                owner_subject_id: owner.subject_id.clone(),
                session_id: binding.session_id.clone(),
                runtime_id: runtime.runtime_id.clone(),
                runtime_epoch: hex_bytes(&runtime.epoch),
                runtime_generation: runtime.generation,
                run_id: String::new(),
                turn_id: String::new(),
                semantic_epoch: hex_bytes(&epoch),
                seq: 0,
                emitted_unix_ms: 0,
                op: "snapshot_reset".into(),
                entity_kind: "session".into(),
                entity_id: binding.session_id.clone(),
                payload,
                payload_sha256: String::new(),
                append_offset_utf8_bytes: None,
            });
        }
        let retained_tail = journal.tail_seq();
        let retained_head = if journal.retained_events() == 0 {
            0
        } else {
            retained_tail.saturating_sub(journal.retained_events() as u64 - 1)
        };
        let restore = !request.snapshot_json.is_empty()
            || request.transcript_revision > 0
            || !request.config_json.is_empty();
        let driver_result = if restore {
            runtime_handle.restore_session(
                binding.session_id.clone(),
                json!({
                    "workspace_id": binding.workspace_id,
                    "snapshot_sha256": hex_bytes(&Sha256::digest(&request.snapshot_json)),
                    "snapshot_bytes": request.snapshot_json.len(),
                    "transcript_revision": request.transcript_revision,
                    "config_sha256": hex_bytes(&Sha256::digest(&request.config_json)),
                    "config_bytes": request.config_json.len(),
                }),
            )
        } else {
            runtime_handle.open_session(
                binding.session_id.clone(),
                json!({
                "workspace_id": binding.workspace_id,
                // The driver receives only bounded restore metadata here.  A
                // later host callback will stream canonical snapshot content;
                // copying up to the 1 MiB RPC snapshot into a 256 KiB driver
                // envelope would be both lossy and an avoidable memory spike.
                "snapshot_sha256": hex_bytes(&Sha256::digest(&request.snapshot_json)),
                "snapshot_bytes": request.snapshot_json.len(),
                "transcript_revision": request.transcript_revision,
                "config_sha256": hex_bytes(&Sha256::digest(&request.config_json)),
                "config_bytes": request.config_json.len(),
                }),
            )
        };
        if let Err(error) = driver_result {
            if !matches!(error, crate::runtime_actor::ActorError::DriverNotAttached) {
                return Ok(Response::new(OpenSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "driver_unavailable",
                        "driver session could not be opened",
                        "driver",
                        true,
                    )),
                    ..Default::default()
                }));
            }
        }
        sessions.insert(
            session_key,
            SessionEntry {
                binding: binding.clone(),
                semantic_epoch: epoch.clone(),
                journal,
            },
        );
        Ok(Response::new(OpenSessionResponse {
            session: Some(binding),
            semantic_epoch: epoch,
            retained_head,
            retained_tail,
            terminals: Vec::<TerminalDescriptor>::new(),
            ..Default::default()
        }))
    }

    async fn close_session(
        &self,
        request: Request<CloseSessionRequest>,
    ) -> Result<Response<CloseSessionResponse>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                return Ok(Response::new(CloseSessionResponse {
                    error: Some(error),
                    ..Default::default()
                }))
            }
        };
        let Some(session) = request.session.as_ref() else {
            return Ok(Response::new(CloseSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "session binding is required",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        };
        let Some(runtime) = session.runtime.as_ref() else {
            return Ok(Response::new(CloseSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "runtime binding is required",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        };
        if session.session_id.is_empty()
            || session.session_id.len() > 128
            || !owner_binding_matches(runtime.owner.as_ref(), owner)
        {
            return Ok(Response::new(CloseSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "forbidden",
                    "session binding is not permitted",
                    "authority",
                    false,
                )),
                ..Default::default()
            }));
        }
        let handle = {
            let runtimes = self
                .runtimes
                .lock()
                .map_err(|_| Status::internal("runtime registry unavailable"))?;
            let Some(entry) = runtimes.values().find(|entry| entry.binding == *runtime) else {
                return Ok(Response::new(CloseSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "stale_generation",
                        "runtime binding is not current",
                        "runtime_actor",
                        false,
                    )),
                    ..Default::default()
                }));
            };
            entry.handle.clone()
        };
        let session_key = format!("{}\u{1f}{}", runtime.runtime_id, session.session_id);
        {
            let sessions = self
                .sessions
                .lock()
                .map_err(|_| Status::internal("session registry unavailable"))?;
            let Some(entry) = sessions.get(&session_key) else {
                return Ok(Response::new(CloseSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "stale_session",
                        "session binding is not current",
                        "runtime_actor",
                        false,
                    )),
                    ..Default::default()
                }));
            };
            if entry.binding != *session {
                return Ok(Response::new(CloseSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "stale_session",
                        "session binding is not current",
                        "runtime_actor",
                        false,
                    )),
                    ..Default::default()
                }));
            }
        }
        match handle.close_session(session.session_id.clone()) {
            Ok(response) if response.ok => {}
            Err(crate::runtime_actor::ActorError::DriverNotAttached) => {}
            Ok(response) => {
                let driver_error = response.error;
                return Ok(Response::new(CloseSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        driver_error
                            .as_ref()
                            .map(|error| error.code.as_str())
                            .unwrap_or("driver_protocol_error"),
                        driver_error
                            .as_ref()
                            .map(|error| error.safe_message.as_str())
                            .unwrap_or("driver rejected session close"),
                        "driver",
                        driver_error
                            .as_ref()
                            .map(|error| error.retryable)
                            .unwrap_or(false),
                    )),
                    ..Default::default()
                }));
            }
            Err(_) => {
                return Ok(Response::new(CloseSessionResponse {
                    error: Some(safe_error(
                        request_id,
                        "unavailable",
                        "driver session close is unavailable",
                        "driver",
                        true,
                    )),
                    ..Default::default()
                }));
            }
        }
        let mut sessions = self
            .sessions
            .lock()
            .map_err(|_| Status::internal("session registry unavailable"))?;
        let Some(entry) = sessions.get(&session_key) else {
            return Ok(Response::new(CloseSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_session",
                    "session binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        };
        if entry.binding != *session {
            return Ok(Response::new(CloseSessionResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_session",
                    "session binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        sessions.remove(&session_key);
        Ok(Response::new(CloseSessionResponse {
            closed: true,
            ..Default::default()
        }))
    }

    async fn attach_semantic(
        &self,
        request: Request<protocol::AttachSemanticRequest>,
    ) -> Result<Response<Self::AttachSemanticStream>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Error(error)),
                })]);
                return Ok(Response::new(Box::pin(stream)));
            }
        };
        let session = match request.session.as_ref() {
            Some(session) => session,
            None => {
                let error = safe_error(
                    request_id,
                    "malformed_request",
                    "session binding is required",
                    "semantic",
                    false,
                );
                let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Error(error)),
                })]);
                return Ok(Response::new(Box::pin(stream)));
            }
        };
        if request.semantic_epoch.len() != 16
            || session.session_id.is_empty()
            || session.runtime.is_none()
            || !owner_binding_matches(
                session
                    .runtime
                    .as_ref()
                    .and_then(|runtime| runtime.owner.as_ref()),
                owner,
            )
        {
            let error = safe_error(
                request_id,
                "malformed_request",
                "semantic session binding is malformed",
                "semantic",
                false,
            );
            let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                item: Some(protocol::semantic_stream_item::Item::Error(error)),
            })]);
            return Ok(Response::new(Box::pin(stream)));
        }
        let grade = if request.grade.is_empty() {
            "block"
        } else {
            request.grade.as_str()
        };
        if !matches!(grade, "off" | "turn" | "block" | "delta") {
            let error = safe_error(
                request_id,
                "malformed_request",
                "semantic subscription grade is invalid",
                "semantic",
                false,
            );
            let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                item: Some(protocol::semantic_stream_item::Item::Error(error)),
            })]);
            return Ok(Response::new(Box::pin(stream)));
        }
        let runtime = session.runtime.as_ref().expect("validated runtime");
        let session_key = format!("{}\u{1f}{}", runtime.runtime_id, session.session_id);
        let sessions = self
            .sessions
            .lock()
            .map_err(|_| Status::internal("session registry unavailable"))?;
        let Some(entry) = sessions.get(&session_key) else {
            let error = safe_error(
                request_id,
                "stale_session",
                "semantic session is not current",
                "semantic",
                false,
            );
            let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                item: Some(protocol::semantic_stream_item::Item::Error(error)),
            })]);
            return Ok(Response::new(Box::pin(stream)));
        };
        if entry.binding != *session {
            let error = safe_error(
                request_id,
                "stale_session",
                "semantic session binding is not current",
                "semantic",
                false,
            );
            let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                item: Some(protocol::semantic_stream_item::Item::Error(error)),
            })]);
            return Ok(Response::new(Box::pin(stream)));
        }
        let mut epoch = [0_u8; 16];
        epoch.copy_from_slice(&request.semantic_epoch);
        let events = match entry
            .journal
            .replay(&SemanticEpoch(epoch), request.since_seq)
        {
            Ok(events) => events,
            Err(crate::semantic::SemanticJournalError::WrongEpoch) => {
                let error = safe_error(
                    request_id,
                    "stale_epoch",
                    "semantic epoch is not current",
                    "semantic",
                    false,
                );
                let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Error(error)),
                })]);
                return Ok(Response::new(Box::pin(stream)));
            }
            Err(crate::semantic::SemanticJournalError::CursorAhead) => {
                let error = safe_error(
                    request_id,
                    "malformed_request",
                    "semantic cursor is ahead of the journal",
                    "semantic",
                    false,
                );
                let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Error(error)),
                })]);
                return Ok(Response::new(Box::pin(stream)));
            }
            Err(crate::semantic::SemanticJournalError::ReplayGap { oldest_seq }) => {
                let error = safe_error(
                    request_id,
                    "replay_gap",
                    &format!("semantic replay starts at sequence {oldest_seq}"),
                    "semantic",
                    false,
                );
                let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Error(error)),
                })]);
                return Ok(Response::new(Box::pin(stream)));
            }
            Err(
                crate::semantic::SemanticJournalError::PayloadNotObject
                | crate::semantic::SemanticJournalError::PayloadTooLarge,
            ) => {
                let error = safe_error(
                    request_id,
                    "internal",
                    "semantic journal could not be replayed",
                    "semantic",
                    false,
                );
                let stream = tokio_stream::iter(vec![Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Error(error)),
                })]);
                return Ok(Response::new(Box::pin(stream)));
            }
        };
        let items = events
            .into_iter()
            .filter_map(|event| semantic_event_for_grade(event, grade))
            .map(|event| {
                Ok(SemanticStreamItem {
                    item: Some(protocol::semantic_stream_item::Item::Event(
                        WireSemanticEvent {
                            owner_subject_id: event.owner_subject_id,
                            session_id: event.session_id,
                            runtime_id: event.runtime_id,
                            runtime_epoch: event.runtime_epoch,
                            runtime_generation: event.runtime_generation,
                            run_id: event.run_id,
                            turn_id: event.turn_id,
                            semantic_epoch: decode_hex(&event.semantic_epoch),
                            seq: event.seq,
                            emitted_unix_ms: event.emitted_unix_ms,
                            op: event.op,
                            entity_kind: event.entity_kind,
                            entity_id: event.entity_id,
                            payload_json: serde_json::to_vec(&event.payload).unwrap_or_default(),
                            payload_sha256: decode_hex(&event.payload_sha256),
                            append_offset_utf8_bytes: event.append_offset_utf8_bytes.unwrap_or(0),
                        },
                    )),
                })
            })
            .collect::<Vec<_>>();
        Ok(Response::new(Box::pin(tokio_stream::iter(items))))
    }

    async fn ack_semantic(
        &self,
        request: Request<AckSemanticRequest>,
    ) -> Result<Response<AckSemanticResponse>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                return Ok(Response::new(AckSemanticResponse {
                    error: Some(error),
                    ..Default::default()
                }))
            }
        };
        let session = match request.session.as_ref() {
            Some(session) => session,
            None => {
                return Ok(Response::new(AckSemanticResponse {
                    error: Some(safe_error(
                        request_id,
                        "malformed_request",
                        "session binding is required",
                        "semantic",
                        false,
                    )),
                    ..Default::default()
                }));
            }
        };
        let Some(runtime) = session.runtime.as_ref() else {
            return Ok(Response::new(AckSemanticResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "runtime binding is required",
                    "semantic",
                    false,
                )),
                ..Default::default()
            }));
        };
        if request.semantic_epoch.len() != 16
            || !owner_binding_matches(runtime.owner.as_ref(), owner)
        {
            return Ok(Response::new(AckSemanticResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "semantic binding is malformed",
                    "semantic",
                    false,
                )),
                ..Default::default()
            }));
        }
        let session_key = format!("{}\u{1f}{}", runtime.runtime_id, session.session_id);
        let sessions = self
            .sessions
            .lock()
            .map_err(|_| Status::internal("session registry unavailable"))?;
        let Some(entry) = sessions.get(&session_key) else {
            return Ok(Response::new(AckSemanticResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_session",
                    "semantic session is not current",
                    "semantic",
                    false,
                )),
                ..Default::default()
            }));
        };
        if entry.binding != *session {
            return Ok(Response::new(AckSemanticResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_session",
                    "semantic session binding is not current",
                    "semantic",
                    false,
                )),
                ..Default::default()
            }));
        }
        let mut epoch = [0_u8; 16];
        epoch.copy_from_slice(&request.semantic_epoch);
        if entry.journal.epoch() != SemanticEpoch(epoch) {
            return Ok(Response::new(AckSemanticResponse {
                error: Some(safe_error(
                    request_id,
                    "stale_epoch",
                    "semantic epoch is not current",
                    "semantic",
                    false,
                )),
                ..Default::default()
            }));
        }
        if request.highest_contiguous_seq > entry.journal.tail_seq() {
            return Ok(Response::new(AckSemanticResponse {
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "semantic ACK is ahead of the journal",
                    "semantic",
                    false,
                )),
                ..Default::default()
            }));
        }
        Ok(Response::new(AckSemanticResponse {
            accepted_seq: request.highest_contiguous_seq,
            ..Default::default()
        }))
    }

    async fn run_turn(
        &self,
        request: Request<TurnRequest>,
    ) -> Result<Response<Self::RunTurnStream>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                    TurnStreamItem {
                        item: Some(protocol::turn_stream_item::Item::Error(error)),
                    },
                )]))))
            }
        };
        let turn = match request.turn.as_ref() {
            Some(turn) => turn,
            None => {
                let error = safe_error(
                    request_id,
                    "malformed_request",
                    "turn binding is required",
                    "turn",
                    false,
                );
                return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                    TurnStreamItem {
                        item: Some(protocol::turn_stream_item::Item::Error(error)),
                    },
                )]))));
            }
        };
        let session = match turn.session.as_ref() {
            Some(session) => session,
            None => {
                let error = safe_error(
                    request_id,
                    "malformed_request",
                    "session binding is required",
                    "turn",
                    false,
                );
                return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                    TurnStreamItem {
                        item: Some(protocol::turn_stream_item::Item::Error(error)),
                    },
                )]))));
            }
        };
        let Some(runtime) = session.runtime.as_ref() else {
            let error = safe_error(
                request_id,
                "malformed_request",
                "runtime binding is required",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        };
        if turn.run_id.is_empty()
            || turn.run_id.len() > 128
            || turn.turn_id.is_empty()
            || turn.turn_id.len() > 128
            || !owner_binding_matches(runtime.owner.as_ref(), owner)
        {
            let error = safe_error(
                request_id,
                "malformed_request",
                "turn binding is malformed",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        if !matches!(
            protocol::InteractionMode::try_from(request.mode),
            Ok(protocol::InteractionMode::Chat)
                | Ok(protocol::InteractionMode::Plan)
                | Ok(protocol::InteractionMode::Agent)
        ) {
            let error = safe_error(
                request_id,
                "malformed_request",
                "turn interaction mode is invalid",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        if request.prompt_json.len() > 1024 * 1024 || request.driver_config_json.len() > 1024 * 1024
        {
            let error = safe_error(
                request_id,
                "malformed_request",
                "turn payload exceeds the bounded limit",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        if (!request.transcript_sha256.is_empty() && request.transcript_sha256.len() != 32)
            || (!request.plan_sha256.is_empty() && request.plan_sha256.len() != 32)
        {
            let error = safe_error(
                request_id,
                "malformed_request",
                "turn authority digest is malformed",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        if !request.prompt_sha256.is_empty()
            && (request.prompt_sha256.len() != 32
                || Sha256::digest(&request.prompt_json).as_slice() != request.prompt_sha256)
        {
            let error = safe_error(
                request_id,
                "malformed_request",
                "turn prompt digest does not match",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        let payload = if request.prompt_json.is_empty() {
            serde_json::json!({})
        } else {
            match serde_json::from_slice::<serde_json::Value>(&request.prompt_json) {
                Ok(value) if value.is_object() => value,
                _ => {
                    let error = safe_error(
                        request_id,
                        "malformed_request",
                        "turn prompt must be a JSON object",
                        "turn",
                        false,
                    );
                    return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                        TurnStreamItem {
                            item: Some(protocol::turn_stream_item::Item::Error(error)),
                        },
                    )]))));
                }
            }
        };
        let session_key = format!("{}\u{1f}{}", runtime.runtime_id, session.session_id);
        let sessions = self
            .sessions
            .lock()
            .map_err(|_| Status::internal("session registry unavailable"))?;
        if !sessions.contains_key(&session_key) {
            let error = safe_error(
                request_id,
                "stale_session",
                "turn session is not current",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        if sessions.get(&session_key).map(|entry| &entry.binding) != Some(session) {
            let error = safe_error(
                request_id,
                "stale_session",
                "turn session binding is not current",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        }
        drop(sessions);
        let runtimes = self
            .runtimes
            .lock()
            .map_err(|_| Status::internal("runtime registry unavailable"))?;
        let Some(entry) = runtimes.values().find(|entry| entry.binding == *runtime) else {
            let error = safe_error(
                request_id,
                "stale_generation",
                "turn runtime is not current",
                "turn",
                false,
            );
            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                },
            )]))));
        };
        let handle = entry.handle.clone();
        drop(runtimes);
        let items = match handle.submit_turn(
            session.session_id.clone(),
            payload,
            turn.run_id.clone(),
            turn.turn_id.clone(),
        ) {
            Ok(response) if response.ok => {
                let session_key = format!("{}\u{1f}{}", runtime.runtime_id, session.session_id);
                let mut sessions = self
                    .sessions
                    .lock()
                    .map_err(|_| Status::internal("session registry unavailable"))?;
                let Some(entry) = sessions.get_mut(&session_key) else {
                    let error = safe_error(
                        request_id,
                        "stale_session",
                        "turn session is not current",
                        "turn",
                        false,
                    );
                    return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                        TurnStreamItem {
                            item: Some(protocol::turn_stream_item::Item::Error(error)),
                        },
                    )]))));
                };
                if entry.binding != *session {
                    let error = safe_error(
                        request_id,
                        "stale_session",
                        "turn session binding is not current",
                        "turn",
                        false,
                    );
                    return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                        TurnStreamItem {
                            item: Some(protocol::turn_stream_item::Item::Error(error)),
                        },
                    )]))));
                }
                let semantic_epoch = hex_bytes(&entry.semantic_epoch);
                let events = match driver_events(
                    response.payload.as_ref(),
                    owner,
                    session,
                    &semantic_epoch,
                    &turn.run_id,
                    &turn.turn_id,
                ) {
                    Ok(events) => events,
                    Err(message) => {
                        let error = safe_error(
                            request_id,
                            "driver_protocol_error",
                            message,
                            "driver",
                            false,
                        );
                        return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                            TurnStreamItem {
                                item: Some(protocol::turn_stream_item::Item::Error(error)),
                            },
                        )]))));
                    }
                };
                let mut stored = Vec::with_capacity(events.len());
                for event in events {
                    match entry.journal.append(event) {
                        Ok(event) => stored.push(event),
                        Err(_) => {
                            let error = safe_error(
                                request_id,
                                "driver_protocol_error",
                                "driver semantic event exceeded the bounded journal",
                                "driver",
                                false,
                            );
                            return Ok(Response::new(Box::pin(tokio_stream::iter(vec![Ok(
                                TurnStreamItem {
                                    item: Some(protocol::turn_stream_item::Item::Error(error)),
                                },
                            )]))));
                        }
                    }
                }
                stored
                    .into_iter()
                    .map(|event| {
                        Ok(TurnStreamItem {
                            item: Some(protocol::turn_stream_item::Item::Event(
                                WireSemanticEvent {
                                    owner_subject_id: event.owner_subject_id,
                                    session_id: event.session_id,
                                    runtime_id: event.runtime_id,
                                    runtime_epoch: event.runtime_epoch,
                                    runtime_generation: event.runtime_generation,
                                    run_id: event.run_id,
                                    turn_id: event.turn_id,
                                    semantic_epoch: decode_hex(&event.semantic_epoch),
                                    seq: event.seq,
                                    emitted_unix_ms: event.emitted_unix_ms,
                                    op: event.op,
                                    entity_kind: event.entity_kind,
                                    entity_id: event.entity_id,
                                    payload_json: serde_json::to_vec(&event.payload)
                                        .unwrap_or_default(),
                                    payload_sha256: decode_hex(&event.payload_sha256),
                                    append_offset_utf8_bytes: event
                                        .append_offset_utf8_bytes
                                        .unwrap_or(0),
                                },
                            )),
                        })
                    })
                    .collect::<Vec<_>>()
            }
            Ok(response) => {
                let driver_error = response.error;
                vec![Ok(TurnStreamItem {
                    item: Some(protocol::turn_stream_item::Item::Error(safe_error(
                        request_id,
                        driver_error
                            .as_ref()
                            .map(|error| error.code.as_str())
                            .unwrap_or("driver_protocol_error"),
                        driver_error
                            .as_ref()
                            .map(|error| error.safe_message.as_str())
                            .unwrap_or("driver rejected the turn"),
                        "driver",
                        driver_error
                            .as_ref()
                            .map(|error| error.retryable)
                            .unwrap_or(false),
                    ))),
                })]
            }
            Err(crate::runtime_actor::ActorError::DriverNotAttached) => vec![Ok(TurnStreamItem {
                item: Some(protocol::turn_stream_item::Item::Error(safe_error(
                    request_id,
                    "not_ready",
                    "driver process is not attached",
                    "driver",
                    true,
                ))),
            })],
            Err(_) => vec![Ok(TurnStreamItem {
                item: Some(protocol::turn_stream_item::Item::Error(safe_error(
                    request_id,
                    "unavailable",
                    "driver turn is unavailable",
                    "driver",
                    true,
                ))),
            })],
        };
        Ok(Response::new(Box::pin(tokio_stream::iter(items))))
    }

    async fn activate_driver_callback(
        &self,
        request: Request<ActivateDriverCallbackRequest>,
    ) -> Result<Response<ActivateDriverCallbackResponse>, Status> {
        let request = request.into_inner();
        let (owner, request_id) = match request_meta(request.meta.as_ref()) {
            Ok(value) => value,
            Err(error) => {
                return Ok(Response::new(ActivateDriverCallbackResponse {
                    capabilities: runtime_capabilities(),
                    error: Some(error),
                    ..Default::default()
                }));
            }
        };
        let binding = match request.runtime.as_ref() {
            Some(binding) => binding,
            None => {
                return Ok(Response::new(ActivateDriverCallbackResponse {
                    capabilities: runtime_capabilities(),
                    error: Some(safe_error(
                        request_id,
                        "malformed_request",
                        "runtime binding is required",
                        "runtime_actor",
                        false,
                    )),
                    ..Default::default()
                }));
            }
        };
        let bound_owner = binding.owner.as_ref();
        if !owner_binding_matches(bound_owner, owner)
            || binding.runtime_id.is_empty()
            || binding.epoch.len() != 16
            || binding.fingerprint.len() != 32
        {
            return Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "stale_generation",
                    "runtime binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        if request.callback_registration_id.len() != 32
            || !request
                .callback_registration_id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || request.driver_pid == 0
            || request.driver_start_token.is_empty()
            || request.callback_endpoint.is_empty()
            || request.callback_endpoint.len() > 512
            || request.callback_endpoint.contains('\0')
            || request.callback_nonce.len() != 32
            || request.callback_binding_sha256.len() != 32
        {
            return Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "malformed_request",
                    "callback binding is malformed",
                    "driver",
                    false,
                )),
                ..Default::default()
            }));
        }
        let runtimes = self
            .runtimes
            .lock()
            .map_err(|_| Status::internal("runtime registry unavailable"))?;
        let entry = runtimes
            .values()
            .find(|entry| entry.binding.runtime_id == binding.runtime_id);
        let Some(entry) = entry else {
            return Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "stale_generation",
                    "runtime binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        };
        if entry.binding != *binding {
            return Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "stale_generation",
                    "runtime binding is not current",
                    "runtime_actor",
                    false,
                )),
                ..Default::default()
            }));
        }
        if entry.driver_pid == 0 {
            return Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "not_ready",
                    "driver process is not attached",
                    "driver",
                    true,
                )),
                ..Default::default()
            }));
        }
        if entry.driver_pid != request.driver_pid
            || entry.driver_start_token != request.driver_start_token
            || entry.callback_registration_id != request.callback_registration_id
        {
            return Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "stale_generation",
                    "driver identity is not current",
                    "driver",
                    false,
                )),
                ..Default::default()
            }));
        }
        let handle = entry.handle.clone();
        drop(runtimes);
        let payload = json!({
            "registration_id": request.callback_registration_id,
            "callback_endpoint": request.callback_endpoint,
            "callback_nonce": base64::engine::general_purpose::URL_SAFE_NO_PAD
                .encode(&request.callback_nonce),
            "callback_binding_sha256": hex_bytes(&request.callback_binding_sha256),
        });
        match handle.activate_driver(payload) {
            Ok(response) => {
                let mut capabilities = attached_runtime_capabilities();
                capabilities.extend(driver_capabilities(response.payload.as_ref()));
                Ok(Response::new(ActivateDriverCallbackResponse {
                    driver_ready: response
                        .payload
                        .as_ref()
                        .and_then(|payload| payload.get("ready"))
                        .and_then(|value| value.as_bool())
                        .unwrap_or(false),
                    capabilities,
                    ..Default::default()
                }))
            }
            Err(crate::runtime_actor::ActorError::DriverNotAttached) => {
                Ok(Response::new(ActivateDriverCallbackResponse {
                    capabilities: runtime_capabilities(),
                    error: Some(safe_error(
                        request_id,
                        "not_ready",
                        "driver process is not attached",
                        "driver",
                        true,
                    )),
                    ..Default::default()
                }))
            }
            Err(_) => Ok(Response::new(ActivateDriverCallbackResponse {
                capabilities: runtime_capabilities(),
                error: Some(safe_error(
                    request_id,
                    "unavailable",
                    "driver activation is unavailable",
                    "driver",
                    true,
                )),
                ..Default::default()
            })),
        }
    }
}

#[cfg(unix)]
pub async fn serve_unix(
    socket_path: &Path,
    expected_session: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    serve_unix_with_service(socket_path, expected_session, HealthOnlyService::default()).await
}

#[cfg(unix)]
pub async fn serve_unix_with_service(
    socket_path: &Path,
    expected_session: &str,
    service: HealthOnlyService,
) -> Result<(), Box<dyn std::error::Error>> {
    use protocol::agent_supervisor_server::AgentSupervisorServer;
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};
    use tokio::net::UnixListener;
    use tokio_stream::wrappers::UnixListenerStream;
    use tonic::transport::server::UdsConnectInfo;

    let parent = socket_path
        .parent()
        .ok_or_else(|| "socket path has no parent")?;
    std::fs::create_dir_all(parent)?;
    std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
    if socket_path.exists() {
        let metadata = std::fs::symlink_metadata(socket_path)?;
        if !metadata.file_type().is_socket() {
            return Err("refusing to replace non-socket runtime endpoint".into());
        }
        std::fs::remove_file(socket_path)?;
    }
    let listener = UnixListener::bind(socket_path)?;
    std::fs::set_permissions(socket_path, std::fs::Permissions::from_mode(0o600))?;
    let incoming = UnixListenerStream::new(listener);
    if expected_session.is_empty() {
        return Err("supervisor session binding is required".into());
    }
    let session_value: MetadataValue<_> = expected_session.parse()?;
    let expected_uid = nix::unistd::geteuid().as_raw();
    let service = tonic::service::interceptor::InterceptedService::new(
        AgentSupervisorServer::new(service),
        move |request: Request<()>| {
            if request.metadata().get("x-open-clank-session") != Some(&session_value) {
                return Err(Status::unauthenticated("invalid local session binding"));
            }
            let peer_uid = request
                .extensions()
                .get::<UdsConnectInfo>()
                .and_then(|info| info.peer_cred.as_ref())
                .map(|cred| cred.uid());
            if peer_uid != Some(expected_uid) {
                return Err(Status::unauthenticated("invalid local peer identity"));
            }
            Ok(request)
        },
    );
    let result = tonic::transport::Server::builder()
        .add_service(service)
        .serve_with_incoming_shutdown(incoming, shutdown_signal())
        .await;
    let _ = std::fs::remove_file(socket_path);
    result?;
    let _ = schema_sha256();
    Ok(())
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {},
            _ = terminate.recv() => {},
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

#[cfg(not(unix))]
pub async fn serve_unix(_socket_path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    Err("Unix-domain transport is unavailable on this platform".into())
}

#[cfg(not(unix))]
pub async fn serve_unix_with_service(
    _socket_path: &Path,
    _expected_session: &str,
    _service: HealthOnlyService,
) -> Result<(), Box<dyn std::error::Error>> {
    Err("Unix-domain transport is unavailable on this platform".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{
        AckSemanticRequest, ActivateDriverCallbackRequest, AttachSemanticRequest,
        CloseSessionRequest, OpenOwnerRuntimeRequest, OpenSessionRequest, OwnerRef,
        ProtocolVersion, RequestMeta, TurnRequest,
    };
    use tokio_stream::StreamExt;

    fn meta(subject_id: &str, request_id: &str) -> RequestMeta {
        RequestMeta {
            protocol: Some(ProtocolVersion {
                major: 1,
                minor: 0,
                schema_sha256: schema_sha256().into_bytes(),
                features: vec!["runtime_actor".into()],
            }),
            request_id: request_id.into(),
            idempotency_key: request_id.into(),
            owner: Some(OwnerRef {
                subject_id: subject_id.into(),
                username: format!("{subject_id}@example.test"),
                auth_generation: 3,
            }),
            deadline_unix_ms: 0,
            authority: Vec::new(),
        }
    }

    #[test]
    fn semantic_grade_filters_without_breaking_sequence_identity() {
        let mut event = SemanticEvent {
            seq: 7,
            op: "text_append".into(),
            entity_kind: "message".into(),
            entity_id: "message-1".into(),
            payload: json!({"text": "hello"}),
            append_offset_utf8_bytes: Some(5),
            ..Default::default()
        };
        let filtered = semantic_event_for_grade(event.clone(), "block").expect("placeholder");
        assert_eq!(filtered.seq, 7);
        assert_eq!(filtered.op, "filtered");
        assert!(filtered.entity_kind.is_empty());
        assert_eq!(filtered.payload, json!({}));
        assert!(filtered.append_offset_utf8_bytes.is_none());
        assert!(semantic_event_for_grade(event.clone(), "off").is_none());
        assert_eq!(semantic_event_for_grade(event.clone(), "delta"), Some(event.clone()));
        event.op = "turn_upsert".into();
        assert_eq!(semantic_event_for_grade(event.clone(), "turn"), Some(event));
    }

    #[test]
    fn driver_event_operations_are_allowlisted_and_append_offsets_are_typed() {
        let owner = OwnerRef {
            subject_id: "subject-events".into(),
            username: "events@example.test".into(),
            auth_generation: 3,
        };
        let session = SessionBinding {
            runtime: Some(RuntimeBinding {
                owner: Some(owner.clone()),
                runtime_id: "runtime-events".into(),
                epoch: vec![1; 16],
                generation: 2,
                fingerprint: vec![2; 32],
                adapter_id: "fixture".into(),
            }),
            session_id: "session-events".into(),
            workspace_id: "workspace-events".into(),
        };
        let unknown = driver_events(
            Some(&json!({
                "events": [{
                    "op": "make_secret",
                    "entity_kind": "message",
                    "entity_id": "m1",
                    "payload": {}
                }]
            })),
            &owner,
            &session,
            "a".repeat(32).as_str(),
            "run-1",
            "turn-1",
        );
        assert_eq!(unknown, Err("driver semantic event operation is not supported"));
        let missing_offset = driver_events(
            Some(&json!({
                "events": [{
                    "op": "text_append",
                    "entity_kind": "message",
                    "entity_id": "m1",
                    "payload": {"text": "hello"}
                }]
            })),
            &owner,
            &session,
            "a".repeat(32).as_str(),
            "run-1",
            "turn-1",
        );
        assert_eq!(
            missing_offset,
            Err("driver text append requires a UTF-8 offset")
        );
    }

    #[tokio::test]
    async fn owner_runtime_rpc_reuses_exact_generation_binding() {
        let service = HealthOnlyService::default();
        let first = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-a", "request-1")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![7; 32],
                expected_generation: 4,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(first.error.is_none());
        assert!(!first.driver_ready);
        let first_binding = first.runtime.clone().expect("binding");
        assert_eq!(first_binding.generation, 4);
        assert_eq!(first_binding.fingerprint, vec![7; 32]);

        let second = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-a", "request-2")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![7; 32],
                expected_generation: 4,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_eq!(second.runtime.expect("binding"), first_binding);

        let replacement = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-a", "request-3")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![8; 32],
                expected_generation: 5,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_ne!(
            replacement.runtime.expect("binding").runtime_id,
            first_binding.runtime_id
        );

        let mut new_auth = meta("subject-a", "request-4");
        new_auth.owner.as_mut().expect("owner").auth_generation = 4;
        let auth_replacement = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(new_auth),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![7; 32],
                expected_generation: 4,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_ne!(
            auth_replacement.runtime.expect("binding").runtime_id,
            first_binding.runtime_id
        );
    }

    #[tokio::test]
    async fn callback_activation_is_typed_not_ready_until_a_driver_is_attached() {
        let service = HealthOnlyService::default();
        let opened = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-a", "request-1")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![7; 32],
                expected_generation: 4,
            }))
            .await
            .expect("rpc")
            .into_inner();
        let runtime = opened.runtime.expect("binding");
        let response = service
            .activate_driver_callback(Request::new(ActivateDriverCallbackRequest {
                meta: Some(meta("subject-a", "request-2")),
                runtime: Some(runtime.clone()),
                callback_registration_id: "a".repeat(32),
                driver_pid: 42,
                driver_start_token: "start-token".into(),
                callback_endpoint: "/private/callback.sock".into(),
                callback_nonce: vec![9; 32],
                callback_binding_sha256: vec![10; 32],
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(!response.driver_ready);
        let error = response.error.expect("typed error");
        assert_eq!(error.code, "not_ready");
        assert_eq!(error.phase, "driver");
        assert!(error.retryable);
    }

    #[tokio::test]
    async fn session_open_binds_runtime_workspace_and_snapshot_digest() {
        let service = HealthOnlyService::default();
        let opened = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-session", "request-runtime")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![4; 32],
                expected_generation: 2,
            }))
            .await
            .expect("rpc")
            .into_inner();
        let runtime = opened.runtime.expect("runtime");
        let snapshot = br#"{"transcript":[]}"#;
        let snapshot_hash = Sha256::digest(snapshot).to_vec();
        let first = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-session", "request-session")),
                runtime: Some(runtime.clone()),
                session_id: "session-1".into(),
                workspace_id: "workspace-1".into(),
                snapshot_json: snapshot.to_vec(),
                snapshot_sha256: snapshot_hash,
                transcript_revision: 7,
                config_json: br#"{}"#.to_vec(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(first.error.is_none());
        assert_eq!(first.retained_head, 1);
        assert_eq!(first.retained_tail, 1);
        assert_eq!(first.semantic_epoch.len(), 16);
        let binding = first.session.clone().expect("session");
        assert_eq!(binding.workspace_id, "workspace-1");

        let mut stale_auth = meta("subject-session", "request-session-stale-auth");
        stale_auth.owner.as_mut().expect("owner").auth_generation = 4;
        let denied = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(stale_auth),
                runtime: Some(runtime.clone()),
                session_id: "session-stale-auth".into(),
                workspace_id: "workspace-1".into(),
                snapshot_json: Vec::new(),
                snapshot_sha256: Vec::new(),
                transcript_revision: 0,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_eq!(denied.error.expect("stale auth error").code, "forbidden");

        let second = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-session", "request-session-2")),
                runtime: Some(runtime),
                session_id: "session-1".into(),
                workspace_id: "workspace-1".into(),
                snapshot_json: Vec::new(),
                snapshot_sha256: Vec::new(),
                transcript_revision: 99,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_eq!(second.session.expect("session"), binding);
        assert_eq!(second.semantic_epoch, first.semantic_epoch);
    }

    #[tokio::test]
    async fn session_close_removes_exact_binding_and_is_idempotently_stale_afterward() {
        let service = HealthOnlyService::default();
        let runtime = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-close", "request-runtime")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![6; 32],
                expected_generation: 2,
            }))
            .await
            .expect("rpc")
            .into_inner()
            .runtime
            .expect("runtime");
        let session = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-close", "request-open")),
                runtime: Some(runtime.clone()),
                session_id: "session-close".into(),
                workspace_id: "workspace-close".into(),
                snapshot_json: Vec::new(),
                snapshot_sha256: Vec::new(),
                transcript_revision: 0,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner()
            .session
            .expect("session");
        let closed = service
            .close_session(Request::new(CloseSessionRequest {
                meta: Some(meta("subject-close", "request-close")),
                session: Some(session.clone()),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(closed.error.is_none());
        assert!(closed.closed);

        let stale = service
            .close_session(Request::new(CloseSessionRequest {
                meta: Some(meta("subject-close", "request-close-again")),
                session: Some(session),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_eq!(stale.error.expect("stale session").code, "stale_session");
        assert!(!stale.closed);
    }

    #[tokio::test]
    async fn semantic_attach_replays_snapshot_and_reports_epoch_gaps() {
        let service = HealthOnlyService::default();
        let runtime = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-semantic", "request-runtime")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![5; 32],
                expected_generation: 2,
            }))
            .await
            .expect("rpc")
            .into_inner()
            .runtime
            .expect("runtime");
        let snapshot = br#"{"messages":[]}"#;
        let opened = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-semantic", "request-session")),
                runtime: Some(runtime.clone()),
                session_id: "session-semantic".into(),
                workspace_id: "workspace-semantic".into(),
                snapshot_json: snapshot.to_vec(),
                snapshot_sha256: Sha256::digest(snapshot).to_vec(),
                transcript_revision: 1,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let session = opened.session.expect("session");
        let mut stream = service
            .attach_semantic(Request::new(AttachSemanticRequest {
                meta: Some(meta("subject-semantic", "request-attach")),
                session: Some(session.clone()),
                semantic_epoch: opened.semantic_epoch.clone(),
                since_seq: 0,
                grade: "block".into(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let item = stream.next().await.expect("snapshot event").expect("item");
        match item.item.expect("oneof") {
            protocol::semantic_stream_item::Item::Event(event) => {
                assert_eq!(event.seq, 1);
                assert_eq!(event.op, "snapshot_reset");
                assert_eq!(event.payload_json, snapshot);
            }
            protocol::semantic_stream_item::Item::Error(error) => {
                panic!("unexpected error: {error:?}")
            }
        }
        let mut off_stream = service
            .attach_semantic(Request::new(AttachSemanticRequest {
                meta: Some(meta("subject-semantic", "request-off")),
                session: Some(session.clone()),
                semantic_epoch: opened.semantic_epoch.clone(),
                since_seq: 0,
                grade: "off".into(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(off_stream.next().await.is_none());
        let mut gap_stream = service
            .attach_semantic(Request::new(AttachSemanticRequest {
                meta: Some(meta("subject-semantic", "request-gap")),
                session: Some(session.clone()),
                semantic_epoch: opened.semantic_epoch.clone(),
                since_seq: 9,
                grade: "block".into(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let gap = gap_stream.next().await.expect("gap event").expect("item");
        match gap.item.expect("oneof") {
            protocol::semantic_stream_item::Item::Error(error) => {
                assert_eq!(error.code, "malformed_request");
            }
            protocol::semantic_stream_item::Item::Event(_) => panic!("unexpected event"),
        }
        let accepted = service
            .ack_semantic(Request::new(AckSemanticRequest {
                meta: Some(meta("subject-semantic", "request-ack")),
                session: Some(session.clone()),
                semantic_epoch: opened.semantic_epoch.clone(),
                highest_contiguous_seq: 1,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(accepted.error.is_none());
        assert_eq!(accepted.accepted_seq, 1);
        let ahead = service
            .ack_semantic(Request::new(AckSemanticRequest {
                meta: Some(meta("subject-semantic", "request-ack-ahead")),
                session: Some(session),
                semantic_epoch: opened.semantic_epoch,
                highest_contiguous_seq: 9,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert_eq!(ahead.error.expect("typed error").code, "malformed_request");
    }

    #[tokio::test]
    async fn run_turn_reaches_actor_and_stays_typed_not_ready_without_driver() {
        let service = HealthOnlyService::default();
        let runtime = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-turn", "request-runtime")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![3; 32],
                expected_generation: 2,
            }))
            .await
            .expect("rpc")
            .into_inner()
            .runtime
            .expect("runtime");
        let session = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-turn", "request-session")),
                runtime: Some(runtime.clone()),
                session_id: "session-turn".into(),
                workspace_id: "workspace-turn".into(),
                snapshot_json: Vec::new(),
                snapshot_sha256: Vec::new(),
                transcript_revision: 0,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner()
            .session
            .expect("session");
        let mut invalid_mode = service
            .run_turn(Request::new(TurnRequest {
                meta: Some(meta("subject-turn", "request-invalid-mode")),
                turn: Some(protocol::TurnBinding {
                    session: Some(session.clone()),
                    run_id: "run-invalid".into(),
                    turn_id: "turn-invalid".into(),
                }),
                mode: 0,
                prompt_json: br#"{"prompt":"hello"}"#.to_vec(),
                prompt_sha256: Sha256::digest(br#"{"prompt":"hello"}"#).to_vec(),
                transcript_sha256: Vec::new(),
                transcript_revision: 0,
                plan_sha256: Vec::new(),
                plan_revision: 0,
                driver_config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let invalid_mode_item = invalid_mode
            .next()
            .await
            .expect("invalid mode result")
            .expect("item");
        match invalid_mode_item.item.expect("oneof") {
            protocol::turn_stream_item::Item::Error(error) => {
                assert_eq!(error.code, "malformed_request")
            }
            protocol::turn_stream_item::Item::Event(_) => panic!("invalid mode reached driver"),
        }
        let mut invalid_digest = service
            .run_turn(Request::new(TurnRequest {
                meta: Some(meta("subject-turn", "request-invalid-digest")),
                turn: Some(protocol::TurnBinding {
                    session: Some(session.clone()),
                    run_id: "run-invalid-digest".into(),
                    turn_id: "turn-invalid-digest".into(),
                }),
                mode: 3,
                prompt_json: br#"{"prompt":"hello"}"#.to_vec(),
                prompt_sha256: Sha256::digest(br#"{"prompt":"hello"}"#).to_vec(),
                transcript_sha256: vec![7; 31],
                transcript_revision: 1,
                plan_sha256: Vec::new(),
                plan_revision: 0,
                driver_config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let invalid_digest_item = invalid_digest
            .next()
            .await
            .expect("invalid digest result")
            .expect("item");
        match invalid_digest_item.item.expect("oneof") {
            protocol::turn_stream_item::Item::Error(error) => {
                assert_eq!(error.code, "malformed_request")
            }
            protocol::turn_stream_item::Item::Event(_) => panic!("invalid digest reached driver"),
        }
        let mut stream = service
            .run_turn(Request::new(TurnRequest {
                meta: Some(meta("subject-turn", "request-turn")),
                turn: Some(protocol::TurnBinding {
                    session: Some(session),
                    run_id: "run-1".into(),
                    turn_id: "turn-1".into(),
                }),
                mode: 3,
                prompt_json: br#"{"prompt":"hello"}"#.to_vec(),
                prompt_sha256: Sha256::digest(br#"{"prompt":"hello"}"#).to_vec(),
                transcript_sha256: Vec::new(),
                transcript_revision: 0,
                plan_sha256: Vec::new(),
                plan_revision: 0,
                driver_config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let item = stream.next().await.expect("turn result").expect("item");
        match item.item.expect("oneof") {
            protocol::turn_stream_item::Item::Error(error) => assert_eq!(error.code, "not_ready"),
            protocol::turn_stream_item::Item::Event(_) => panic!("unexpected semantic event"),
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn configured_driver_is_spawned_attached_and_activated_by_the_actor() {
        use std::path::Path;

        let python = ["/usr/bin/python3", "/opt/homebrew/bin/python3"]
            .into_iter()
            .find(|path| Path::new(path).is_file())
            .expect("python fixture");
        let script = r#"
import json, os, struct
def read_exact(n):
    data = bytearray()
    while len(data) < n:
        part = os.read(3, n-len(data))
        if not part: return None
        data.extend(part)
    return bytes(data)
def send(value):
    body = json.dumps(value, separators=(',', ':')).encode()
    os.write(3, struct.pack('>I', len(body)) + body)
while True:
    header = read_exact(4)
    if header is None: break
    body = read_exact(struct.unpack('>I', header)[0])
    if body is None: break
    request = json.loads(body)
    command = request['command']
    if command == 'hello':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'hello_ack', 'payload': {'ready': False, 'capabilities': {}}})
    elif command == 'activate':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'activated', 'payload': {'callback_ready': True, 'ready': False, 'capabilities': {'semantic_transcript': False}}})
    elif command == 'open_session':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'accepted', 'payload': {'session_open': True}})
    elif command == 'restore_session':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'accepted', 'payload': {'session_restored': True}})
    elif command == 'submit_turn':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'accepted', 'payload': {'events': [{'op': 'message_upsert', 'entity_kind': 'message', 'entity_id': request['turn_id'], 'payload': {'text': 'fixture event'}}]}})
    elif command == 'shutdown':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'shutdown_ack', 'payload': {'closed': True}})
        break
"#;
        let service = HealthOnlyService::with_driver_launch(DriverLaunchSpec {
            program: python.into(),
            args: vec!["-c".into(), script.into()],
            cwd: "/tmp".into(),
        });
        let opened = service
            .open_owner_runtime(Request::new(OpenOwnerRuntimeRequest {
                meta: Some(meta("subject-driver", "request-driver")),
                adapter_id: "mimo".into(),
                projection_fingerprint: vec![6; 32],
                expected_generation: 9,
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(opened.error.is_none());
        assert!(opened.driver_pid > 0);
        assert!(!opened.driver_start_token.is_empty());
        assert_eq!(opened.callback_registration_id.len(), 32);
        assert!(opened
            .capabilities
            .iter()
            .any(|capability| capability.name == "driver" && capability.supported));
        let runtime = opened.runtime.expect("binding");
        let session = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-driver", "request-session")),
                runtime: Some(runtime.clone()),
                session_id: "session-driver".into(),
                workspace_id: "workspace-driver".into(),
                snapshot_json: Vec::new(),
                snapshot_sha256: Vec::new(),
                transcript_revision: 0,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner()
            .session
            .expect("session");
        let activated = service
            .activate_driver_callback(Request::new(ActivateDriverCallbackRequest {
                meta: Some(meta("subject-driver", "request-activate")),
                runtime: Some(runtime.clone()),
                callback_registration_id: opened.callback_registration_id,
                driver_pid: opened.driver_pid,
                driver_start_token: opened.driver_start_token,
                callback_endpoint: "/tmp/callback.sock".into(),
                callback_nonce: vec![8; 32],
                callback_binding_sha256: vec![9; 32],
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(activated.error.is_none());
        assert!(!activated.driver_ready);
        assert!(activated
            .capabilities
            .iter()
            .any(|capability| capability.name == "semantic_transcript" && !capability.supported));
        let reopened = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-driver", "request-session-reopen")),
                runtime: Some(runtime.clone()),
                session_id: "session-driver".into(),
                workspace_id: "workspace-driver".into(),
                snapshot_json: Vec::new(),
                snapshot_sha256: Vec::new(),
                transcript_revision: 1,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(reopened.error.is_none());
        assert_eq!(reopened.session.expect("reopened").session_id, "session-driver");
        let restore_snapshot = br#"{"messages":[]}"#;
        let restored = service
            .open_session(Request::new(OpenSessionRequest {
                meta: Some(meta("subject-driver", "request-session-restore")),
                runtime: Some(runtime.clone()),
                session_id: "session-driver".into(),
                workspace_id: "workspace-driver".into(),
                snapshot_json: restore_snapshot.to_vec(),
                snapshot_sha256: Sha256::digest(restore_snapshot).to_vec(),
                transcript_revision: 2,
                config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        assert!(restored.error.is_none());
        let mut turn = service
            .run_turn(Request::new(TurnRequest {
                meta: Some(meta("subject-driver", "request-turn")),
                turn: Some(protocol::TurnBinding {
                    session: Some(session),
                    run_id: "run-driver".into(),
                    turn_id: "turn-driver".into(),
                }),
                mode: 3,
                prompt_json: br#"{"prompt":"hello"}"#.to_vec(),
                prompt_sha256: Sha256::digest(br#"{"prompt":"hello"}"#).to_vec(),
                transcript_sha256: Vec::new(),
                transcript_revision: 0,
                plan_sha256: Vec::new(),
                plan_revision: 0,
                driver_config_json: Vec::new(),
            }))
            .await
            .expect("rpc")
            .into_inner();
        let result = turn.next().await.expect("turn result").expect("item");
        match result.item.expect("oneof") {
            protocol::turn_stream_item::Item::Event(event) => {
                assert_eq!(event.op, "message_upsert");
                assert_eq!(event.entity_id, "turn-driver");
                assert_eq!(event.seq, 1);
            }
            protocol::turn_stream_item::Item::Error(error) => panic!("unexpected error: {error:?}"),
        }
    }
}
