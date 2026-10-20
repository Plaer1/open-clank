use crate::driver_wire::{DriverCommand, DriverEnvelope, DriverResponse, DriverResponseError};
use serde_json::{json, Value};
use std::collections::BTreeSet;

#[derive(Debug, Clone, PartialEq, Eq)]
struct DriverIdentity {
    owner_subject_id: String,
    runtime_id: String,
    runtime_epoch: String,
    runtime_generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ActivationBinding {
    registration_id: String,
    callback_binding_sha256: String,
}

/// Rust-side lifecycle shell for the direct driver.
///
/// It is deliberately transport-neutral: the service owns framing and
/// process identity, while this state machine owns hello/activation fencing.
/// It reports callback readiness separately from turn readiness until the
/// retained engine can execute a host-authorized turn.
#[derive(Debug, Default)]
pub struct DriverSession {
    identity: Option<DriverIdentity>,
    activation: Option<ActivationBinding>,
    sessions: BTreeSet<String>,
    closed: bool,
}

impl DriverSession {
    pub fn handle(&mut self, envelope: DriverEnvelope) -> DriverResponse {
        if self.closed {
            return error_response(
                &envelope.request_id,
                "driver_closed",
                "driver is closed",
                false,
            );
        }
        if self.identity.is_none() {
            if envelope.command != DriverCommand::Hello {
                return error_response(
                    &envelope.request_id,
                    "driver_not_initialized",
                    "driver hello is required",
                    false,
                );
            }
            self.identity = Some(DriverIdentity {
                owner_subject_id: envelope.owner_subject_id,
                runtime_id: envelope.runtime_id,
                runtime_epoch: envelope.runtime_epoch,
                runtime_generation: envelope.runtime_generation,
            });
            return success_response(
                &envelope.request_id,
                "hello_ack",
                json!({"ready": false, "reason": "host_activation_required", "capabilities": {}}),
            );
        }
        if !same_identity(self.identity.as_ref().expect("identity set"), &envelope) {
            return error_response(
                &envelope.request_id,
                "stale_runtime",
                "driver runtime identity is stale",
                false,
            );
        }
        if envelope.command == DriverCommand::Activate {
            let Some(binding) = activation_binding(&envelope.payload) else {
                return error_response(
                    &envelope.request_id,
                    "activation_invalid",
                    "driver activation is invalid",
                    false,
                );
            };
            if self
                .activation
                .as_ref()
                .is_some_and(|current| current != &binding)
            {
                return error_response(
                    &envelope.request_id,
                    "activation_stale",
                    "driver activation is stale",
                    false,
                );
            }
            self.activation = Some(binding);
            return success_response(
                &envelope.request_id,
                "activated",
                json!({
                    "callback_ready": true,
                    "ready": false,
                    "reason": "turn_execution_not_implemented",
                    "capabilities": {}
                }),
            );
        }
        if matches!(envelope.command, DriverCommand::OpenSession | DriverCommand::RestoreSession) {
            if envelope.session_id.is_empty() || envelope.session_id.len() > 128 {
                return error_response(
                    &envelope.request_id,
                    "session_invalid",
                    "driver session binding is invalid",
                    false,
                );
            }
            self.sessions.insert(envelope.session_id.clone());
            return success_response(
                &envelope.request_id,
                "accepted",
                if envelope.command == DriverCommand::RestoreSession {
                    json!({"session_restored": true})
                } else {
                    json!({"session_open": true})
                },
            );
        }
        if envelope.command == DriverCommand::Close {
            if !self.sessions.remove(&envelope.session_id) {
                return error_response(
                    &envelope.request_id,
                    "session_not_open",
                    "driver session is not open",
                    false,
                );
            }
            return success_response(
                &envelope.request_id,
                "session_closed",
                json!({"session_closed": true}),
            );
        }
        if envelope.command == DriverCommand::SubmitTurn
            && !self.sessions.contains(&envelope.session_id)
        {
            return error_response(
                &envelope.request_id,
                "session_not_open",
                "driver session is not open",
                false,
            );
        }
        if envelope.command == DriverCommand::Shutdown {
            self.closed = true;
            return success_response(
                &envelope.request_id,
                "shutdown_ack",
                json!({"closed": true}),
            );
        }
        if self.activation.is_none() {
            return error_response(
                &envelope.request_id,
                "driver_not_activated",
                "host activation is required before turn traffic",
                true,
            );
        }
        error_response(
            &envelope.request_id,
            "driver_not_ready",
            "driver turn execution is not ready",
            true,
        )
    }
}

fn activation_binding(payload: &Value) -> Option<ActivationBinding> {
    let object = payload.as_object()?;
    let registration_id = object.get("registration_id")?.as_str()?;
    let callback_binding_sha256 = object.get("callback_binding_sha256")?.as_str()?;
    Some(ActivationBinding {
        registration_id: registration_id.to_owned(),
        callback_binding_sha256: callback_binding_sha256.to_owned(),
    })
}

fn same_identity(left: &DriverIdentity, right: &DriverEnvelope) -> bool {
    left.owner_subject_id == right.owner_subject_id
        && left.runtime_id == right.runtime_id
        && left.runtime_epoch == right.runtime_epoch
        && left.runtime_generation == right.runtime_generation
}

fn success_response(request_id: &str, event: &str, payload: Value) -> DriverResponse {
    DriverResponse {
        schema_version: 1,
        request_id: request_id.to_owned(),
        ok: true,
        event: Some(event.to_owned()),
        payload: Some(payload),
        error: None,
    }
}

fn error_response(
    request_id: &str,
    code: &str,
    safe_message: &str,
    retryable: bool,
) -> DriverResponse {
    DriverResponse {
        schema_version: 1,
        request_id: request_id.to_owned(),
        ok: false,
        event: None,
        payload: None,
        error: Some(DriverResponseError {
            code: code.to_owned(),
            safe_message: safe_message.to_owned(),
            retryable,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::driver_wire::decode;

    fn envelope(command: &str, payload: Value) -> DriverEnvelope {
        let value = json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "owner_subject_id": "subject-1",
            "session_id": "session-1",
            "runtime_id": "runtime-1",
            "runtime_epoch": "b".repeat(32),
            "runtime_generation": 2,
            "command": command,
            "payload": payload
        });
        decode(&serde_json::to_vec(&value).expect("json")).expect("envelope")
    }

    fn activation() -> Value {
        json!({
            "registration_id": "e".repeat(32),
            "callback_endpoint": "/tmp/callback.sock",
            "callback_nonce": "N".repeat(43),
            "callback_binding_sha256": "f".repeat(64)
        })
    }

    #[test]
    fn hello_activation_and_shutdown_are_identity_fenced() {
        let mut session = DriverSession::default();
        assert_eq!(
            session
                .handle(envelope("submit_turn", json!({})))
                .error
                .unwrap()
                .code,
            "driver_not_initialized"
        );
        assert_eq!(
            session
                .handle(envelope(
                    "hello",
                    json!({
                        "protocol_major": 1,
                        "protocol_minor": 0,
                        "schema_sha256": "c".repeat(64),
                        "binding": "d".repeat(64)
                    })
                ))
                .event
                .as_deref(),
            Some("hello_ack")
        );
        assert_eq!(
            session
                .handle(envelope("activate", activation()))
                .event
                .as_deref(),
            Some("activated")
        );
        assert_eq!(
            session
                .handle(envelope("submit_turn", json!({})))
                .error
                .unwrap()
                .code,
            "session_not_open"
        );
        assert_eq!(
            session
                .handle(envelope("open_session", json!({"workspace_id": "workspace-1"})))
                .event
                .as_deref(),
            Some("accepted")
        );
        assert_eq!(
            session
                .handle(envelope("submit_turn", json!({})))
                .error
                .unwrap()
                .code,
            "driver_not_ready"
        );
        assert_eq!(
            session
                .handle(envelope("close", json!({})))
                .event
                .as_deref(),
            Some("session_closed")
        );
        assert_eq!(
            session
                .handle(envelope("submit_turn", json!({})))
                .error
                .unwrap()
                .code,
            "session_not_open"
        );
        assert_eq!(
            session
                .handle(envelope("shutdown", json!({})))
                .event
                .as_deref(),
            Some("shutdown_ack")
        );
        assert_eq!(
            session
                .handle(envelope("shutdown", json!({})))
                .error
                .unwrap()
                .code,
            "driver_closed"
        );
    }

    #[test]
    fn activation_replacement_and_identity_drift_fail_closed() {
        let mut session = DriverSession::default();
        session.handle(envelope(
            "hello",
            json!({
                "protocol_major": 1,
                "protocol_minor": 0,
                "schema_sha256": "c".repeat(64),
                "binding": "d".repeat(64)
            }),
        ));
        session.handle(envelope("activate", activation()));
        let mut changed = activation();
        changed["registration_id"] = Value::String("1".repeat(32));
        assert_eq!(
            session
                .handle(envelope("activate", changed))
                .error
                .unwrap()
                .code,
            "activation_stale"
        );
        let mut drift = envelope("submit_turn", json!({}));
        drift.owner_subject_id = "other".into();
        assert_eq!(session.handle(drift).error.unwrap().code, "stale_runtime");
    }

    #[test]
    fn per_command_session_binding_does_not_reject_a_shared_runtime_driver() {
        let mut session = DriverSession::default();
        session.handle(envelope(
            "hello",
            json!({
                "protocol_major": 1,
                "protocol_minor": 0,
                "schema_sha256": "c".repeat(64),
                "binding": "d".repeat(64)
            }),
        ));
        session.handle(envelope("activate", activation()));

        let mut second_session = envelope("submit_turn", json!({}));
        second_session.session_id = "session-2".into();
        let mut open_second = envelope("open_session", json!({"workspace_id": "workspace-2"}));
        open_second.session_id = "session-2".into();
        assert_eq!(session.handle(open_second).event.as_deref(), Some("accepted"));
        assert_eq!(
            session.handle(second_session).error.unwrap().code,
            "driver_not_ready"
        );
    }
}
