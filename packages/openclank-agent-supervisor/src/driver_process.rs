//! Host-owned launch and control of one retained direct-driver process.
//!
//! This is deliberately a small, synchronous process boundary. The owner
//! runtime actor must serialize calls to a `DirectDriverProcess`; no caller
//! may write arbitrary bytes to the inherited descriptor. The process is
//! admitted only after a bound hello response, and callback activation is a
//! separate host-authorized step. Turn execution remains fail-closed until a
//! later driver implementation reports real capabilities.

use crate::driver_control::{DriverControlError, DriverControlProcess};
use crate::driver_wire::{DriverCommand, DriverEnvelope, DriverResponse};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriverIdentity {
    pub owner_subject_id: String,
    pub session_id: String,
    pub runtime_id: String,
    pub runtime_epoch: String,
    pub runtime_generation: u64,
}

#[derive(Debug, Error)]
pub enum DriverProcessError {
    #[error("driver process control failed: {0}")]
    Control(#[from] DriverControlError),
    #[error("driver process spawn failed: {0}")]
    Spawn(String),
    #[error("driver handshake failed: {0}")]
    Handshake(String),
    #[error("driver response was not successful: {0}")]
    Rejected(String),
    #[error("driver process is already closed")]
    Closed,
}

#[cfg(unix)]
pub struct DirectDriverProcess {
    transport: DriverTransport,
    identity: DriverIdentity,
    schema_sha256: String,
    binding_sha256: String,
    next_request_id: u64,
    closed: bool,
}

#[cfg(unix)]
enum DriverTransport {
    Control(DriverControlProcess),
    Pty(crate::process::PtyControlProcess),
}

#[cfg(unix)]
impl DriverTransport {
    fn exchange(
        &mut self,
        envelope: &DriverEnvelope,
    ) -> Result<DriverResponse, DriverControlError> {
        match self {
            Self::Control(process) => process.exchange(envelope),
            Self::Pty(process) => process.exchange(envelope),
        }
    }

    fn pid(&self) -> u32 {
        match self {
            Self::Control(process) => process.pid(),
            Self::Pty(process) => process.pid(),
        }
    }

    fn start_token(&self) -> Result<String, DriverControlError> {
        match self {
            Self::Control(process) => process.start_token(),
            Self::Pty(process) => crate::process::process_start_token(process.pid())
                .map_err(|error| DriverControlError::Spawn(error.to_string())),
        }
    }

    fn kill_group(&mut self) -> Result<(), DriverControlError> {
        match self {
            Self::Control(process) => process.kill_group(),
            Self::Pty(process) => process
                .kill_group()
                .map_err(|error| DriverControlError::Spawn(error.to_string())),
        }
    }

    fn wait(&mut self) -> Result<(), DriverControlError> {
        match self {
            Self::Control(process) => process.wait().map(|_| ()),
            Self::Pty(process) => process
                .wait()
                .map(|_| ())
                .map_err(|error| DriverControlError::Spawn(error.to_string())),
        }
    }
}

#[cfg(unix)]
impl DirectDriverProcess {
    /// Spawn the retained engine's `openclank-driver` command and complete its
    /// identity-bound hello before returning. `binding_sha256` is a launch
    /// binding, not a credential; callers should generate a fresh value for
    /// every process and never place it in argv or environment.
    pub fn spawn(
        spec: &crate::process::SpawnSpec,
        allowed_environment: &[&str],
        identity: DriverIdentity,
        schema_sha256: impl Into<String>,
        binding_sha256: impl Into<String>,
    ) -> Result<Self, DriverProcessError> {
        let schema_sha256 = schema_sha256.into();
        let binding_sha256 = binding_sha256.into();
        if !is_lower_hex(&schema_sha256, 64) || !is_lower_hex(&binding_sha256, 64) {
            return Err(DriverProcessError::Handshake(
                "driver schema/binding identity is malformed".to_owned(),
            ));
        }
        Self::admit(
            DriverTransport::Control(DriverControlProcess::spawn(spec, allowed_environment)?),
            identity,
            schema_sha256,
            binding_sha256,
        )
    }

    /// Spawn the same direct driver with stdio attached to a PTY while the
    /// structured protocol remains on its private fd 3. This is the runtime
    /// transport used by the future terminal actor; it shares the exact hello
    /// and activation admission with the control-only path.
    pub fn spawn_pty(
        spec: &crate::process::SpawnSpec,
        allowed_environment: &[&str],
        rows: u16,
        cols: u16,
        identity: DriverIdentity,
        schema_sha256: impl Into<String>,
        binding_sha256: impl Into<String>,
    ) -> Result<Self, DriverProcessError> {
        Self::admit(
            DriverTransport::Pty(
                crate::process::PtyControlProcess::spawn(spec, allowed_environment, rows, cols)
                    .map_err(|error| DriverProcessError::Spawn(error.to_string()))?,
            ),
            identity,
            schema_sha256.into(),
            binding_sha256.into(),
        )
    }

    fn admit(
        transport: DriverTransport,
        identity: DriverIdentity,
        schema_sha256: String,
        binding_sha256: String,
    ) -> Result<Self, DriverProcessError> {
        if !is_lower_hex(&schema_sha256, 64) || !is_lower_hex(&binding_sha256, 64) {
            return Err(DriverProcessError::Handshake(
                "driver schema/binding identity is malformed".to_owned(),
            ));
        }
        let mut process = Self {
            transport,
            identity,
            schema_sha256,
            binding_sha256,
            next_request_id: 0,
            closed: false,
        };
        let response = process.request_internal(
            DriverCommand::Hello,
            json!({
                "protocol_major": 1,
                "protocol_minor": 0,
                "schema_sha256": process.schema_sha256.clone(),
                "binding": process.binding_sha256.clone(),
            }),
            None,
            None,
            None,
            None,
            None,
        )?;
        if !response.ok || response.event.as_deref() != Some("hello_ack") {
            let detail = response
                .error
                .map(|error| error.code)
                .unwrap_or_else(|| "unexpected hello response".to_owned());
            let _ = process.kill_group();
            return Err(DriverProcessError::Handshake(detail));
        }
        Ok(process)
    }

    /// Deliver the host-created callback binding after the Python callback
    /// server has verified the paused driver's PID/start token.
    pub fn activate(&mut self, payload: Value) -> Result<DriverResponse, DriverProcessError> {
        self.request(DriverCommand::Activate, payload, None, None, None, None)
    }

    /// Issue one owner/session/runtime-bound command. The current retained
    /// driver returns a typed `driver_not_ready` error for turn commands; that
    /// response is intentionally returned to the caller rather than treated
    /// as success.
    pub fn request(
        &mut self,
        command: DriverCommand,
        payload: Value,
        run_id: Option<String>,
        turn_id: Option<String>,
        deadline_unix_ms: Option<u64>,
        idempotency_key: Option<String>,
    ) -> Result<DriverResponse, DriverProcessError> {
        self.request_internal(
            command,
            payload,
            None,
            run_id,
            turn_id,
            deadline_unix_ms,
            idempotency_key,
        )
    }

    /// Issue a command for a concrete canonical session while retaining the
    /// owner/runtime identity established by hello.  One admitted driver may
    /// host multiple sessions; the session is therefore a per-command binding
    /// and is never inferred from the process identity.
    pub fn request_for_session(
        &mut self,
        session_id: impl Into<String>,
        command: DriverCommand,
        payload: Value,
        run_id: Option<String>,
        turn_id: Option<String>,
        deadline_unix_ms: Option<u64>,
        idempotency_key: Option<String>,
    ) -> Result<DriverResponse, DriverProcessError> {
        self.request_internal(
            command,
            payload,
            Some(session_id.into()),
            run_id,
            turn_id,
            deadline_unix_ms,
            idempotency_key,
        )
    }

    fn request_internal(
        &mut self,
        command: DriverCommand,
        payload: Value,
        session_id: Option<String>,
        run_id: Option<String>,
        turn_id: Option<String>,
        deadline_unix_ms: Option<u64>,
        idempotency_key: Option<String>,
    ) -> Result<DriverResponse, DriverProcessError> {
        if self.closed {
            return Err(DriverProcessError::Closed);
        }
        let request_id = self.request_id();
        let envelope = DriverEnvelope {
            request_id,
            owner_subject_id: self.identity.owner_subject_id.clone(),
            session_id: session_id.unwrap_or_else(|| self.identity.session_id.clone()),
            runtime_id: self.identity.runtime_id.clone(),
            runtime_epoch: self.identity.runtime_epoch.clone(),
            runtime_generation: self.identity.runtime_generation,
            run_id,
            turn_id,
            command,
            payload,
            deadline_unix_ms,
            idempotency_key,
        };
        self.transport.exchange(&envelope).map_err(Into::into)
    }

    fn request_id(&mut self) -> String {
        self.next_request_id = self.next_request_id.saturating_add(1).max(1);
        request_id_for(&self.identity.runtime_epoch, self.next_request_id)
    }

    pub fn identity(&self) -> &DriverIdentity {
        &self.identity
    }

    pub fn pid(&self) -> u32 {
        self.transport.pid()
    }

    pub fn start_token(&self) -> Result<String, DriverProcessError> {
        self.transport.start_token().map_err(Into::into)
    }

    pub fn kill_group(&mut self) -> Result<(), DriverProcessError> {
        self.transport.kill_group().map_err(Into::into)
    }

    pub fn shutdown(&mut self) -> Result<(), DriverProcessError> {
        if self.closed {
            return Ok(());
        }
        let response = self.request_internal(
            DriverCommand::Shutdown,
            json!({}),
            None,
            None,
            None,
            None,
            None,
        );
        self.closed = true;
        match response? {
            response if response.ok && response.event.as_deref() == Some("shutdown_ack") => {
                let _ = self.transport.wait()?;
                Ok(())
            }
            response => {
                let _ = self.kill_group();
                Err(DriverProcessError::Rejected(
                    response
                        .error
                        .map(|error| error.code)
                        .unwrap_or_else(|| "unexpected shutdown response".to_owned()),
                ))
            }
        }
    }
}

#[cfg(unix)]
impl Drop for DirectDriverProcess {
    fn drop(&mut self) {
        if !self.closed {
            let _ = self.kill_group();
            let _ = self.transport.wait();
            self.closed = true;
        }
    }
}

#[cfg(not(unix))]
pub struct DirectDriverProcess;

#[cfg(not(unix))]
impl DirectDriverProcess {
    pub fn spawn(
        _spec: &crate::process::SpawnSpec,
        _allowed_environment: &[&str],
        _identity: DriverIdentity,
        _schema_sha256: impl Into<String>,
        _binding_sha256: impl Into<String>,
    ) -> Result<Self, DriverProcessError> {
        Err(DriverProcessError::Handshake(
            "direct driver process is not admitted on this platform".to_owned(),
        ))
    }
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn request_id_for(runtime_epoch: &str, sequence: u64) -> String {
    let mut digest = Sha256::new();
    digest.update(runtime_epoch.as_bytes());
    digest.update(sequence.to_be_bytes());
    let bytes = digest.finalize();
    bytes[..16]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> DriverIdentity {
        DriverIdentity {
            owner_subject_id: "subject-1".into(),
            session_id: "session-1".into(),
            runtime_id: "runtime-1".into(),
            runtime_epoch: "b".repeat(32),
            runtime_generation: 2,
        }
    }

    #[test]
    fn request_ids_are_bound_to_runtime_epoch_and_are_distinct() {
        let first = request_id_for(&identity().runtime_epoch, 1);
        let second = request_id_for(&identity().runtime_epoch, 2);
        assert_eq!(first.len(), 32);
        assert!(first.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }

    #[cfg(unix)]
    #[test]
    fn real_child_pty_control_exchange_requires_hello_activation_and_shutdown() {
        use crate::process::SpawnSpec;
        use std::collections::BTreeMap;
        use std::path::PathBuf;

        let python = ["/usr/bin/python3", "/opt/homebrew/bin/python3"]
            .into_iter()
            .find(|path| std::path::Path::new(path).is_file());
        let Some(python) = python else {
            return;
        };
        let script = r#"
import json, os, struct, sys
def read_exact(n):
    data = bytearray()
    while len(data) < n:
        part = os.read(3, n-len(data))
        if not part: return None
        data.extend(part)
    return bytes(data)
def send(value):
    body = json.dumps(value, separators=(',', ':')).encode()
    packet = struct.pack('>I', len(body)) + body
    offset = 0
    while offset < len(packet):
        offset += os.write(3, packet[offset:])
while True:
    header = read_exact(4)
    if header is None: break
    body = read_exact(struct.unpack('>I', header)[0])
    if body is None: break
    request = json.loads(body)
    command = request['command']
    if command == 'hello':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True,
              'event': 'hello_ack', 'payload': {'ready': False, 'capabilities': {}}})
    elif command == 'activate':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True,
              'event': 'activated', 'payload': {'callback_ready': True, 'ready': False, 'capabilities': {}}})
    elif command == 'shutdown':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True,
              'event': 'shutdown_ack', 'payload': {'closed': True}})
        break
    else:
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': False,
              'error': {'code': 'driver_not_ready', 'safe_message': 'not ready', 'retryable': True}})
"#;
        let spec = SpawnSpec {
            program: python.to_owned(),
            args: vec!["-c".into(), script.into()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let mut process = DirectDriverProcess::spawn_pty(
            &spec,
            &[],
            24,
            80,
            identity(),
            "c".repeat(64),
            "d".repeat(64),
        )
        .expect("hello handshake");
        let activation = process
            .activate(json!({
                "registration_id": "e".repeat(32),
                "callback_endpoint": "/tmp/callback.sock",
                "callback_nonce": "N".repeat(43),
                "callback_binding_sha256": "f".repeat(64)
            }))
            .expect("activation exchange");
        assert_eq!(activation.event.as_deref(), Some("activated"));
        let not_ready = process
            .request(DriverCommand::SubmitTurn, json!({}), None, None, None, None)
            .expect("typed not-ready response");
        assert_eq!(
            not_ready.error.as_ref().map(|error| error.code.as_str()),
            Some("driver_not_ready")
        );
        process.shutdown().expect("shutdown exchange");
    }
}
