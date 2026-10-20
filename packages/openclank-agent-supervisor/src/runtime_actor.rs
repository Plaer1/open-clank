use serde_json::Value;
use std::collections::BTreeSet;
use std::sync::mpsc::{self, SyncSender, TrySendError};
use std::thread::{self, JoinHandle};
use thiserror::Error;

use crate::driver_process::DirectDriverProcess;
use crate::driver_wire::{DriverCommand, DriverResponse};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OwnerStatus {
    Ready,
    Draining,
    Retired,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ActorError {
    #[error("owner runtime generation is stale")]
    StaleGeneration,
    #[error("owner runtime is draining")]
    Draining,
    #[error("owner runtime is retired")]
    Retired,
    #[error("owner runtime actor command queue is full")]
    QueueFull,
    #[error("owner runtime actor is unavailable")]
    Unavailable,
    #[error("owner runtime already has a driver")]
    DriverAlreadyAttached,
    #[error("owner runtime has no driver")]
    DriverNotAttached,
    #[error("owner runtime driver is unavailable")]
    DriverUnavailable,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeLease {
    pub owner: String,
    pub generation: u64,
    pub fingerprint: String,
    pub lease_id: u64,
    released: bool,
}

#[derive(Debug, Clone)]
pub struct OwnerRuntime {
    pub owner: String,
    pub generation: u64,
    pub fingerprint: String,
    pub status: OwnerStatus,
    next_lease_id: u64,
    active_leases: BTreeSet<u64>,
}

impl OwnerRuntime {
    pub fn new(owner: impl Into<String>, generation: u64, fingerprint: impl Into<String>) -> Self {
        Self {
            owner: owner.into(),
            generation,
            fingerprint: fingerprint.into(),
            status: OwnerStatus::Ready,
            next_lease_id: 1,
            active_leases: BTreeSet::new(),
        }
    }

    pub fn admit(&mut self, expected_generation: u64) -> Result<RuntimeLease, ActorError> {
        if expected_generation != self.generation {
            return Err(ActorError::StaleGeneration);
        }
        match self.status {
            OwnerStatus::Ready => {}
            OwnerStatus::Draining => return Err(ActorError::Draining),
            OwnerStatus::Retired => return Err(ActorError::Retired),
        }
        let lease_id = self.next_lease_id;
        self.next_lease_id = self.next_lease_id.saturating_add(1);
        self.active_leases.insert(lease_id);
        Ok(RuntimeLease {
            owner: self.owner.clone(),
            generation: self.generation,
            fingerprint: self.fingerprint.clone(),
            lease_id,
            released: false,
        })
    }

    pub fn release(&mut self, lease: &mut RuntimeLease) -> Result<(), ActorError> {
        if lease.released {
            return Ok(());
        }
        if lease.generation != self.generation || lease.owner != self.owner {
            return Err(ActorError::StaleGeneration);
        }
        self.active_leases.remove(&lease.lease_id);
        lease.released = true;
        Ok(())
    }

    pub fn begin_drain(&mut self) {
        if self.status == OwnerStatus::Ready {
            self.status = OwnerStatus::Draining;
        }
    }

    pub fn retire(&mut self) {
        self.status = OwnerStatus::Retired;
        self.active_leases.clear();
    }

    pub fn active_leases(&self) -> usize {
        self.active_leases.len()
    }
}

enum ActorCommand {
    Admit(u64, SyncSender<Result<RuntimeLease, ActorError>>),
    Release(RuntimeLease, SyncSender<Result<RuntimeLease, ActorError>>),
    AttachDriver(DirectDriverProcess, SyncSender<Result<(), ActorError>>),
    ActivateDriver(Value, SyncSender<Result<DriverResponse, ActorError>>),
    OpenSession(
        String,
        Value,
        SyncSender<Result<DriverResponse, ActorError>>,
    ),
    RestoreSession(
        String,
        Value,
        SyncSender<Result<DriverResponse, ActorError>>,
    ),
    CloseSession(String, SyncSender<Result<DriverResponse, ActorError>>),
    SubmitTurn(
        String,
        Value,
        String,
        String,
        SyncSender<Result<DriverResponse, ActorError>>,
    ),
    BeginDrain(SyncSender<Result<(), ActorError>>),
    Retire(SyncSender<Result<(), ActorError>>),
    Shutdown,
}

#[derive(Clone)]
pub struct OwnerRuntimeHandle {
    sender: SyncSender<ActorCommand>,
}

pub struct OwnerRuntimeActor {
    handle: OwnerRuntimeHandle,
    join: Option<JoinHandle<()>>,
}

impl OwnerRuntimeActor {
    pub fn spawn(runtime: OwnerRuntime, queue_capacity: usize) -> Result<Self, ActorError> {
        if queue_capacity == 0 {
            return Err(ActorError::QueueFull);
        }
        let (sender, receiver) = mpsc::sync_channel(queue_capacity);
        let join = thread::Builder::new()
            .name(format!("openclank-owner-runtime-{}", runtime.owner))
            .spawn(move || {
                let mut runtime = runtime;
                let mut driver: Option<DirectDriverProcess> = None;
                while let Ok(command) = receiver.recv() {
                    match command {
                        ActorCommand::Admit(generation, reply) => {
                            let _ = reply.send(runtime.admit(generation));
                        }
                        ActorCommand::Release(mut lease, reply) => {
                            let result = runtime.release(&mut lease).map(|()| lease);
                            let _ = reply.send(result);
                        }
                        ActorCommand::AttachDriver(candidate, reply) => {
                            if runtime.status != OwnerStatus::Ready {
                                drop(candidate);
                                let _ = reply.send(Err(ActorError::Draining));
                            } else if driver.is_some() {
                                drop(candidate);
                                let _ = reply.send(Err(ActorError::DriverAlreadyAttached));
                            } else {
                                driver = Some(candidate);
                                let _ = reply.send(Ok(()));
                            }
                        }
                        ActorCommand::ActivateDriver(payload, reply) => {
                            let result = driver
                                .as_mut()
                                .ok_or(ActorError::DriverNotAttached)
                                .and_then(|process| {
                                    process
                                        .activate(payload)
                                        .map_err(|_| ActorError::DriverUnavailable)
                                });
                            let _ = reply.send(result);
                        }
                        ActorCommand::OpenSession(session_id, payload, reply) => {
                            let result = driver
                                .as_mut()
                                .ok_or(ActorError::DriverNotAttached)
                                .and_then(|process| {
                                    process
                                        .request_for_session(
                                            session_id,
                                            DriverCommand::OpenSession,
                                            payload,
                                            None,
                                            None,
                                            None,
                                            None,
                                        )
                                        .map_err(|_| ActorError::DriverUnavailable)
                                });
                            let _ = reply.send(result);
                        }
                        ActorCommand::RestoreSession(session_id, payload, reply) => {
                            let result = driver
                                .as_mut()
                                .ok_or(ActorError::DriverNotAttached)
                                .and_then(|process| {
                                    process
                                        .request_for_session(
                                            session_id,
                                            DriverCommand::RestoreSession,
                                            payload,
                                            None,
                                            None,
                                            None,
                                            None,
                                        )
                                        .map_err(|_| ActorError::DriverUnavailable)
                                });
                            let _ = reply.send(result);
                        }
                        ActorCommand::CloseSession(session_id, reply) => {
                            let result = driver
                                .as_mut()
                                .ok_or(ActorError::DriverNotAttached)
                                .and_then(|process| {
                                    process
                                        .request_for_session(
                                            session_id,
                                            DriverCommand::Close,
                                            Value::Object(Default::default()),
                                            None,
                                            None,
                                            None,
                                            None,
                                        )
                                        .map_err(|_| ActorError::DriverUnavailable)
                                });
                            let _ = reply.send(result);
                        }
                        ActorCommand::SubmitTurn(session_id, payload, run_id, turn_id, reply) => {
                            let result = driver
                                .as_mut()
                                .ok_or(ActorError::DriverNotAttached)
                                .and_then(|process| {
                                    process
                                        .request_for_session(
                                            session_id,
                                            DriverCommand::SubmitTurn,
                                            payload,
                                            Some(run_id),
                                            Some(turn_id),
                                            None,
                                            None,
                                        )
                                        .map_err(|_| ActorError::DriverUnavailable)
                                });
                            let _ = reply.send(result);
                        }
                        ActorCommand::BeginDrain(reply) => {
                            runtime.begin_drain();
                            let _ = reply.send(Ok(()));
                        }
                        ActorCommand::Retire(reply) => {
                            runtime.retire();
                            if let Some(mut process) = driver.take() {
                                let _ = process.shutdown();
                            }
                            let _ = reply.send(Ok(()));
                        }
                        ActorCommand::Shutdown => {
                            if let Some(mut process) = driver.take() {
                                let _ = process.shutdown();
                            }
                            break;
                        }
                    }
                }
            })
            .map_err(|_| ActorError::Unavailable)?;
        Ok(Self {
            handle: OwnerRuntimeHandle { sender },
            join: Some(join),
        })
    }

    pub fn handle(&self) -> OwnerRuntimeHandle {
        self.handle.clone()
    }

    pub fn shutdown(mut self) -> Result<(), ActorError> {
        self.handle
            .sender
            .send(ActorCommand::Shutdown)
            .map_err(|_| ActorError::Unavailable)?;
        self.join
            .take()
            .ok_or(ActorError::Unavailable)?
            .join()
            .map_err(|_| ActorError::Unavailable)
    }
}

impl Drop for OwnerRuntimeActor {
    fn drop(&mut self) {
        let _ = self.handle.sender.try_send(ActorCommand::Shutdown);
    }
}

impl OwnerRuntimeHandle {
    pub fn admit(&self, expected_generation: u64) -> Result<RuntimeLease, ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::Admit(expected_generation, reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn release(&self, lease: &mut RuntimeLease) -> Result<(), ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        let candidate = lease.clone();
        self.send(ActorCommand::Release(candidate, reply))?;
        *lease = receiver.recv().map_err(|_| ActorError::Unavailable)??;
        Ok(())
    }

    /// Transfer ownership of an already hello-admitted driver to the owner
    /// actor. The candidate is dropped (and therefore killed) on rejection.
    pub fn attach_driver(&self, process: DirectDriverProcess) -> Result<(), ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::AttachDriver(process, reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    /// Serialize callback activation through the owner actor. No caller can
    /// race activation or write the driver's control socket directly.
    pub fn activate_driver(&self, payload: Value) -> Result<DriverResponse, ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::ActivateDriver(payload, reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn open_session(
        &self,
        session_id: String,
        payload: Value,
    ) -> Result<DriverResponse, ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::OpenSession(session_id, payload, reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn restore_session(
        &self,
        session_id: String,
        payload: Value,
    ) -> Result<DriverResponse, ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::RestoreSession(session_id, payload, reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn close_session(&self, session_id: String) -> Result<DriverResponse, ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::CloseSession(session_id, reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn submit_turn(
        &self,
        session_id: String,
        payload: Value,
        run_id: String,
        turn_id: String,
    ) -> Result<DriverResponse, ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::SubmitTurn(
            session_id, payload, run_id, turn_id, reply,
        ))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn begin_drain(&self) -> Result<(), ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::BeginDrain(reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    pub fn retire(&self) -> Result<(), ActorError> {
        let (reply, receiver) = mpsc::sync_channel(1);
        self.send(ActorCommand::Retire(reply))?;
        receiver.recv().map_err(|_| ActorError::Unavailable)?
    }

    fn send(&self, command: ActorCommand) -> Result<(), ActorError> {
        match self.sender.try_send(command) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(_)) => Err(ActorError::QueueFull),
            Err(TrySendError::Disconnected(_)) => Err(ActorError::Unavailable),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generation_and_owner_are_bound_to_lease() {
        let mut runtime = OwnerRuntime::new("alice", 7, "fingerprint-7");
        assert_eq!(runtime.admit(6), Err(ActorError::StaleGeneration));
        let mut lease = runtime.admit(7).expect("admit");
        assert_eq!(runtime.active_leases(), 1);
        runtime.release(&mut lease).expect("release");
        runtime.release(&mut lease).expect("idempotent release");
        assert_eq!(runtime.active_leases(), 0);
    }

    #[test]
    fn draining_and_retired_runtimes_fail_closed() {
        let mut runtime = OwnerRuntime::new("alice", 1, "fingerprint-1");
        runtime.begin_drain();
        assert_eq!(runtime.admit(1), Err(ActorError::Draining));
        runtime.retire();
        assert_eq!(runtime.admit(1), Err(ActorError::Retired));
    }

    #[test]
    fn owner_runtime_actor_fences_generation_and_shutdown() {
        let actor = OwnerRuntimeActor::spawn(OwnerRuntime::new("alice", 7, "fingerprint-7"), 4)
            .expect("actor");
        let handle = actor.handle();
        assert_eq!(handle.admit(6), Err(ActorError::StaleGeneration));
        let mut lease = handle.admit(7).expect("lease");
        handle.release(&mut lease).expect("release");
        handle.begin_drain().expect("drain");
        assert_eq!(handle.admit(7), Err(ActorError::Draining));
        actor.shutdown().expect("shutdown");
        assert_eq!(handle.admit(7), Err(ActorError::Unavailable));
    }

    #[cfg(unix)]
    #[test]
    fn owner_actor_serializes_driver_activation_and_shutdown() {
        use crate::driver_process::DriverIdentity;
        use crate::process::SpawnSpec;
        use std::collections::BTreeMap;
        use std::path::PathBuf;

        let python = ["/usr/bin/python3", "/opt/homebrew/bin/python3"]
            .into_iter()
            .find(|path| std::path::Path::new(path).is_file())
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
    h = read_exact(4)
    if h is None: break
    body = read_exact(struct.unpack('>I', h)[0])
    if body is None: break
    request = json.loads(body)
    command = request['command']
    if command == 'hello':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'hello_ack', 'payload': {'ready': False, 'capabilities': {}}})
    elif command == 'activate':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'activated', 'payload': {'callback_ready': True, 'ready': False, 'capabilities': {}}})
    elif command == 'shutdown':
        send({'schema_version': 1, 'request_id': request['request_id'], 'ok': True, 'event': 'shutdown_ack', 'payload': {'closed': True}})
        break
"#;
        let spec = SpawnSpec {
            program: python.to_owned(),
            args: vec!["-c".into(), script.into()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let process = DirectDriverProcess::spawn(
            &spec,
            &[],
            DriverIdentity {
                owner_subject_id: "subject-1".into(),
                session_id: "session-1".into(),
                runtime_id: "runtime-1".into(),
                runtime_epoch: "b".repeat(32),
                runtime_generation: 2,
            },
            "c".repeat(64),
            "d".repeat(64),
        )
        .expect("hello");
        let actor =
            OwnerRuntimeActor::spawn(OwnerRuntime::new("alice", 2, "fp"), 8).expect("actor");
        let handle = actor.handle();
        handle.attach_driver(process).expect("attach");
        let response = handle
            .activate_driver(serde_json::json!({
                "registration_id": "e".repeat(32),
                "callback_endpoint": "/tmp/callback.sock",
                "callback_nonce": "N".repeat(43),
                "callback_binding_sha256": "f".repeat(64)
            }))
            .expect("activation");
        assert_eq!(response.event.as_deref(), Some("activated"));
        actor.shutdown().expect("actor shutdown");
    }

    #[test]
    fn actor_rejects_an_unbounded_or_zero_queue() {
        assert!(matches!(
            OwnerRuntimeActor::spawn(OwnerRuntime::new("alice", 1, "fp"), 0),
            Err(ActorError::QueueFull)
        ));
    }
}
