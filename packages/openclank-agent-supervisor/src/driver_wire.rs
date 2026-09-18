use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

pub const MAX_DRIVER_ENVELOPE_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DriverCommand {
    Hello,
    Activate,
    OpenSession,
    RestoreSession,
    SubmitTurn,
    CancelTurn,
    SetModel,
    SetMode,
    SetConfig,
    ResolvePermission,
    ResolveQuestion,
    ProviderControl,
    Close,
    Shutdown,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriverEnvelope {
    pub request_id: String,
    pub owner_subject_id: String,
    pub session_id: String,
    pub runtime_id: String,
    pub runtime_epoch: String,
    pub runtime_generation: u64,
    pub run_id: Option<String>,
    pub turn_id: Option<String>,
    pub command: DriverCommand,
    pub payload: Value,
    pub deadline_unix_ms: Option<u64>,
    pub idempotency_key: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DriverResponse {
    pub schema_version: u8,
    pub request_id: String,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub event: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<DriverResponseError>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DriverResponseError {
    pub code: String,
    pub safe_message: String,
    pub retryable: bool,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DriverWireError {
    #[error("driver envelope exceeds the 256 KiB bound")]
    TooLarge,
    #[error("driver envelope is not valid JSON")]
    InvalidJson,
    #[error("driver envelope schema version is unsupported")]
    SchemaVersion,
    #[error("driver envelope contains an unknown field")]
    UnknownField,
    #[error("driver envelope field is missing or malformed: {0}")]
    Field(&'static str),
    #[error("driver envelope command is unknown")]
    Command,
    #[error("driver response is missing a required field: {0}")]
    ResponseField(&'static str),
    #[error("driver response has an invalid success/error shape")]
    ResponseShape,
    #[error("driver response contains a forbidden field")]
    ForbiddenField,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawEnvelope {
    schema_version: u8,
    request_id: String,
    owner_subject_id: String,
    session_id: String,
    runtime_id: String,
    runtime_epoch: String,
    runtime_generation: u64,
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    turn_id: Option<String>,
    command: String,
    payload: Value,
    #[serde(default)]
    deadline_unix_ms: Option<u64>,
    #[serde(default)]
    idempotency_key: Option<String>,
}

pub fn decode(bytes: &[u8]) -> Result<DriverEnvelope, DriverWireError> {
    if bytes.len() > MAX_DRIVER_ENVELOPE_BYTES {
        return Err(DriverWireError::TooLarge);
    }
    let raw: RawEnvelope = serde_json::from_slice(bytes).map_err(|error| {
        if error.to_string().contains("unknown field") {
            DriverWireError::UnknownField
        } else {
            DriverWireError::InvalidJson
        }
    })?;
    if raw.schema_version != 1 {
        return Err(DriverWireError::SchemaVersion);
    }
    require_hex(&raw.request_id, "request_id")?;
    require_text(&raw.owner_subject_id, "owner_subject_id")?;
    require_text(&raw.session_id, "session_id")?;
    require_text(&raw.runtime_id, "runtime_id")?;
    require_hex(&raw.runtime_epoch, "runtime_epoch")?;
    if let Some(key) = &raw.idempotency_key {
        require_hex(key, "idempotency_key")?;
    }
    if !raw.payload.is_object() {
        return Err(DriverWireError::Field("payload"));
    }
    if contains_forbidden_key(&raw.payload) {
        return Err(DriverWireError::ForbiddenField);
    }
    let command = match raw.command.as_str() {
        "hello" => DriverCommand::Hello,
        "activate" => DriverCommand::Activate,
        "open_session" => DriverCommand::OpenSession,
        "restore_session" => DriverCommand::RestoreSession,
        "submit_turn" => DriverCommand::SubmitTurn,
        "cancel_turn" => DriverCommand::CancelTurn,
        "set_model" => DriverCommand::SetModel,
        "set_mode" => DriverCommand::SetMode,
        "set_config" => DriverCommand::SetConfig,
        "resolve_permission" => DriverCommand::ResolvePermission,
        "resolve_question" => DriverCommand::ResolveQuestion,
        "provider_control" => DriverCommand::ProviderControl,
        "close" => DriverCommand::Close,
        "shutdown" => DriverCommand::Shutdown,
        _ => return Err(DriverWireError::Command),
    };
    if matches!(command, DriverCommand::Hello) {
        let object = raw
            .payload
            .as_object()
            .expect("payload object checked above");
        if object.get("protocol_major") != Some(&Value::from(1))
            || object.get("protocol_minor") != Some(&Value::from(0))
            || !matches!(object.get("schema_sha256"), Some(Value::String(value)) if is_lower_hex(value, 64))
            || !matches!(object.get("binding"), Some(Value::String(value)) if is_lower_hex(value, 64))
        {
            return Err(DriverWireError::Field("payload"));
        }
    }
    if matches!(command, DriverCommand::Activate) {
        let object = raw
            .payload
            .as_object()
            .expect("payload object checked above");
        if object.len() != 4
            || !matches!(object.get("registration_id"), Some(Value::String(value)) if is_lower_hex(value, 32))
            || !matches!(object.get("callback_endpoint"), Some(Value::String(value)) if !value.is_empty() && value.len() <= 100 && !value.contains('\0'))
            || !matches!(object.get("callback_nonce"), Some(Value::String(value)) if is_base64url_nonce(value))
            || !matches!(object.get("callback_binding_sha256"), Some(Value::String(value)) if is_lower_hex(value, 64))
        {
            return Err(DriverWireError::Field("payload"));
        }
    }
    Ok(DriverEnvelope {
        request_id: raw.request_id,
        owner_subject_id: raw.owner_subject_id,
        session_id: raw.session_id,
        runtime_id: raw.runtime_id,
        runtime_epoch: raw.runtime_epoch,
        runtime_generation: raw.runtime_generation,
        run_id: raw.run_id,
        turn_id: raw.turn_id,
        command,
        payload: raw.payload,
        deadline_unix_ms: raw.deadline_unix_ms,
        idempotency_key: raw.idempotency_key,
    })
}

/// Encode one host-to-driver envelope with the same validation used on the
/// Rust receive path. Keeping this in the authority crate prevents launch
/// helpers from constructing an unchecked JSON shape or accidentally placing
/// forbidden host data in the driver channel.
pub fn encode(envelope: &DriverEnvelope) -> Result<Vec<u8>, DriverWireError> {
    let command = match envelope.command {
        DriverCommand::Hello => "hello",
        DriverCommand::Activate => "activate",
        DriverCommand::OpenSession => "open_session",
        DriverCommand::RestoreSession => "restore_session",
        DriverCommand::SubmitTurn => "submit_turn",
        DriverCommand::CancelTurn => "cancel_turn",
        DriverCommand::SetModel => "set_model",
        DriverCommand::SetMode => "set_mode",
        DriverCommand::SetConfig => "set_config",
        DriverCommand::ResolvePermission => "resolve_permission",
        DriverCommand::ResolveQuestion => "resolve_question",
        DriverCommand::ProviderControl => "provider_control",
        DriverCommand::Close => "close",
        DriverCommand::Shutdown => "shutdown",
    };
    let value = serde_json::json!({
        "schema_version": 1,
        "request_id": envelope.request_id,
        "owner_subject_id": envelope.owner_subject_id,
        "session_id": envelope.session_id,
        "runtime_id": envelope.runtime_id,
        "runtime_epoch": envelope.runtime_epoch,
        "runtime_generation": envelope.runtime_generation,
        "run_id": envelope.run_id,
        "turn_id": envelope.turn_id,
        "command": command,
        "payload": envelope.payload,
        "deadline_unix_ms": envelope.deadline_unix_ms,
        "idempotency_key": envelope.idempotency_key,
    });
    let bytes = serde_json::to_vec(&value).map_err(|_| DriverWireError::InvalidJson)?;
    if bytes.len() > MAX_DRIVER_ENVELOPE_BYTES {
        return Err(DriverWireError::TooLarge);
    }
    let decoded = decode(&bytes)?;
    if decoded != *envelope {
        return Err(DriverWireError::InvalidJson);
    }
    Ok(bytes)
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawResponse {
    schema_version: u8,
    request_id: String,
    ok: bool,
    #[serde(default)]
    event: Option<String>,
    #[serde(default)]
    payload: Option<Value>,
    #[serde(default)]
    error: Option<RawResponseError>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawResponseError {
    code: String,
    safe_message: String,
    retryable: bool,
}

pub fn encode_response(response: &DriverResponse) -> Result<Vec<u8>, DriverWireError> {
    require_hex(&response.request_id, "request_id")?;
    let bytes = serde_json::to_vec(response).map_err(|_| DriverWireError::InvalidJson)?;
    if bytes.len() > MAX_DRIVER_ENVELOPE_BYTES {
        return Err(DriverWireError::TooLarge);
    }
    decode_response(&bytes)?;
    Ok(bytes)
}

pub fn decode_response(bytes: &[u8]) -> Result<DriverResponse, DriverWireError> {
    if bytes.len() > MAX_DRIVER_ENVELOPE_BYTES {
        return Err(DriverWireError::TooLarge);
    }
    let raw: RawResponse = serde_json::from_slice(bytes).map_err(|error| {
        if error.to_string().contains("unknown field") {
            DriverWireError::UnknownField
        } else {
            DriverWireError::InvalidJson
        }
    })?;
    if raw.schema_version != 1 {
        return Err(DriverWireError::SchemaVersion);
    }
    require_hex(&raw.request_id, "request_id")?;
    if raw.ok {
        if raw.event.as_deref().is_none() || raw.payload.as_ref().is_none() || raw.error.is_some() {
            return Err(DriverWireError::ResponseShape);
        }
        let event = raw.event.expect("event checked above");
        if !matches!(
            event.as_str(),
            "hello_ack" | "activated" | "accepted" | "session_closed" | "shutdown_ack"
        ) {
            return Err(DriverWireError::ResponseField("event"));
        }
        let payload = raw.payload.expect("payload checked above");
        if !payload.is_object() {
            return Err(DriverWireError::ResponseField("payload"));
        }
        if contains_forbidden_key(&payload) {
            return Err(DriverWireError::ForbiddenField);
        }
        return Ok(DriverResponse {
            schema_version: 1,
            request_id: raw.request_id,
            ok: true,
            event: Some(event),
            payload: Some(payload),
            error: None,
        });
    }
    if raw.event.is_some() || raw.payload.is_some() || raw.error.is_none() {
        return Err(DriverWireError::ResponseShape);
    }
    let error = raw.error.expect("error checked above");
    if error.code.is_empty()
        || error.code.len() > 64
        || !error
            .code
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
        || error.safe_message.is_empty()
        || error.safe_message.len() > 256
    {
        return Err(DriverWireError::ResponseField("error"));
    }
    Ok(DriverResponse {
        schema_version: 1,
        request_id: raw.request_id,
        ok: false,
        event: None,
        payload: None,
        error: Some(DriverResponseError {
            code: error.code,
            safe_message: error.safe_message,
            retryable: error.retryable,
        }),
    })
}

fn contains_forbidden_key(value: &Value) -> bool {
    match value {
        Value::Object(object) => object.iter().any(|(key, child)| {
            matches!(
                key.as_str(),
                "credential" | "password" | "secret" | "raw_path" | "cwd"
            ) || contains_forbidden_key(child)
        }),
        Value::Array(values) => values.iter().any(contains_forbidden_key),
        _ => false,
    }
}

fn require_text(value: &str, field: &'static str) -> Result<(), DriverWireError> {
    if value.is_empty() || value.len() > 128 {
        return Err(DriverWireError::Field(field));
    }
    Ok(())
}

fn require_hex(value: &str, field: &'static str) -> Result<(), DriverWireError> {
    if !is_lower_hex(value, 32) {
        return Err(DriverWireError::Field(field));
    }
    Ok(())
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_base64url_nonce(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
}

#[cfg(test)]
mod tests {
    use super::*;

    fn envelope(command: &str) -> Value {
        serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "owner_subject_id": "subject-1",
            "session_id": "session-1",
            "runtime_id": "runtime-1",
            "runtime_epoch": "b".repeat(32),
            "runtime_generation": 2,
            "command": command,
            "payload": {}
        })
    }

    #[test]
    fn direct_driver_commands_are_typed_and_owner_bound() {
        let value = serde_json::to_vec(&envelope("submit_turn")).expect("json");
        let decoded = decode(&value).expect("decode");
        assert_eq!(decoded.command, DriverCommand::SubmitTurn);
        assert_eq!(decoded.owner_subject_id, "subject-1");
        let mut activation = envelope("activate");
        activation["payload"] = serde_json::json!({
            "registration_id": "e".repeat(32),
            "callback_endpoint": "/tmp/callback.sock",
            "callback_nonce": "N".repeat(43),
            "callback_binding_sha256": "f".repeat(64)
        });
        let value = serde_json::to_vec(&activation).expect("json");
        assert_eq!(
            decode(&value).expect("activate").command,
            DriverCommand::Activate
        );
    }

    #[test]
    fn host_encoding_round_trips_without_optional_field_drift() {
        let envelope = DriverEnvelope {
            request_id: "a".repeat(32),
            owner_subject_id: "subject-1".into(),
            session_id: "session-1".into(),
            runtime_id: "runtime-1".into(),
            runtime_epoch: "b".repeat(32),
            runtime_generation: 2,
            run_id: None,
            turn_id: None,
            command: DriverCommand::Shutdown,
            payload: serde_json::json!({}),
            deadline_unix_ms: None,
            idempotency_key: None,
        };
        let bytes = encode(&envelope).expect("encode");
        assert_eq!(decode(&bytes).expect("decode"), envelope);
    }

    #[test]
    fn unknown_fields_commands_and_identity_fail_closed() {
        let mut value = envelope("hello");
        value["credential"] = Value::String("secret".into());
        assert_eq!(
            decode(&serde_json::to_vec(&value).unwrap()),
            Err(DriverWireError::UnknownField)
        );
        let value = envelope("not-a-command");
        assert_eq!(
            decode(&serde_json::to_vec(&value).unwrap()),
            Err(DriverWireError::Command)
        );
        let mut value = envelope("hello");
        value["runtime_epoch"] = Value::String("BAD".into());
        assert_eq!(
            decode(&serde_json::to_vec(&value).unwrap()),
            Err(DriverWireError::Field("runtime_epoch"))
        );
        let mut value = envelope("submit_turn");
        value["payload"] = serde_json::json!({"nested": {"credential": "secret"}});
        assert_eq!(
            decode(&serde_json::to_vec(&value).unwrap()),
            Err(DriverWireError::ForbiddenField)
        );
    }

    #[test]
    fn hello_payload_is_bound_before_a_driver_can_activate() {
        let value = envelope("hello");
        assert_eq!(
            decode(&serde_json::to_vec(&value).unwrap()),
            Err(DriverWireError::Field("payload"))
        );
        let mut valid = envelope("hello");
        valid["payload"] = serde_json::json!({
            "protocol_major": 1,
            "protocol_minor": 0,
            "schema_sha256": "c".repeat(64),
            "binding": "d".repeat(64)
        });
        assert_eq!(
            decode(&serde_json::to_vec(&valid).unwrap())
                .unwrap()
                .command,
            DriverCommand::Hello
        );
    }

    #[test]
    fn activation_payload_is_exactly_bound_and_bounded() {
        let mut valid = envelope("activate");
        valid["payload"] = serde_json::json!({
            "registration_id": "e".repeat(32),
            "callback_endpoint": "/tmp/callback.sock",
            "callback_nonce": "N".repeat(43),
            "callback_binding_sha256": "f".repeat(64)
        });
        assert_eq!(
            decode(&serde_json::to_vec(&valid).unwrap())
                .unwrap()
                .command,
            DriverCommand::Activate
        );
        valid["payload"]["callback_nonce"] = Value::String("bad".into());
        assert_eq!(
            decode(&serde_json::to_vec(&valid).unwrap()),
            Err(DriverWireError::Field("payload"))
        );
        valid["payload"] = serde_json::json!({
            "registration_id": "e".repeat(32),
            "callback_endpoint": "/tmp/callback.sock",
            "callback_nonce": "N".repeat(43),
            "callback_binding_sha256": "f".repeat(64),
            "extra": true
        });
        assert_eq!(
            decode(&serde_json::to_vec(&valid).unwrap()),
            Err(DriverWireError::Field("payload"))
        );
    }

    #[test]
    fn responses_require_typed_success_or_safe_error_shape() {
        let success = serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "ok": true,
            "event": "hello_ack",
            "payload": {"ready": false}
        });
        let decoded = decode_response(&serde_json::to_vec(&success).unwrap()).expect("response");
        assert_eq!(decoded.event.as_deref(), Some("hello_ack"));
        let activated = serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "ok": true,
            "event": "activated",
            "payload": {"callback_ready": true, "ready": false}
        });
        assert_eq!(
            decode_response(&serde_json::to_vec(&activated).unwrap())
                .unwrap()
                .event
                .as_deref(),
            Some("activated")
        );
        let closed = serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "ok": true,
            "event": "session_closed",
            "payload": {"session_closed": true}
        });
        assert_eq!(
            decode_response(&serde_json::to_vec(&closed).unwrap())
                .unwrap()
                .event
                .as_deref(),
            Some("session_closed")
        );
        let failure = serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "ok": false,
            "error": {"code": "driver_not_activated", "safe_message": "not ready", "retryable": true}
        });
        assert_eq!(
            decode_response(&serde_json::to_vec(&failure).unwrap())
                .unwrap()
                .ok,
            false
        );
    }

    #[test]
    fn responses_reject_credentials_and_mixed_shapes() {
        let mut forbidden = serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "ok": true,
            "event": "accepted",
            "payload": {"nested": {"credential": "secret"}}
        });
        assert_eq!(
            decode_response(&serde_json::to_vec(&forbidden).unwrap()),
            Err(DriverWireError::ForbiddenField)
        );
        forbidden["error"] =
            serde_json::json!({"code": "bad", "safe_message": "x", "retryable": false});
        assert_eq!(
            decode_response(&serde_json::to_vec(&forbidden).unwrap()),
            Err(DriverWireError::ResponseShape)
        );
    }
}
