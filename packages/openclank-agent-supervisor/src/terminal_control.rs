use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde_json::Value;
use thiserror::Error;

pub const MAX_TERMINAL_INPUT_BYTES: usize = 64 * 1024;
pub const MAX_TERMINAL_DIMENSION: u16 = 512;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TerminalCommand {
    Attach {
        since_seq: u64,
    },
    Input {
        epoch: String,
        lease_id: String,
        bytes: Vec<u8>,
    },
    Resize {
        epoch: String,
        lease_id: String,
        cols: u16,
        rows: u16,
    },
    Interrupt {
        epoch: String,
    },
    Terminate {
        epoch: String,
    },
    Ack {
        epoch: String,
        ack_seq: u64,
    },
    Takeover {
        epoch: String,
    },
    Detach {
        epoch: String,
    },
    Close {
        epoch: String,
    },
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum TerminalCommandError {
    #[error("terminal control is not a JSON object")]
    NotObject,
    #[error("terminal control has an unsupported schema version")]
    SchemaVersion,
    #[error("terminal control field is missing or malformed: {0}")]
    Field(&'static str),
    #[error("terminal input is not valid base64")]
    InvalidBase64,
    #[error("terminal input exceeds the 64 KiB bound")]
    InputTooLarge,
    #[error("terminal dimensions are outside the 1..=512 bound")]
    Dimensions,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ControllerLeaseError {
    #[error("terminal already has an input controller")]
    AlreadyHeld,
    #[error("terminal controller lease is stale")]
    Stale,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControllerLease {
    holder: Option<String>,
    lease_id: u64,
}

impl Default for ControllerLease {
    fn default() -> Self {
        Self {
            holder: None,
            lease_id: 0,
        }
    }
}

impl ControllerLease {
    pub fn holder(&self) -> Option<&str> {
        self.holder.as_deref()
    }

    pub fn acquire(
        &mut self,
        client_id: &str,
        takeover: bool,
    ) -> Result<u64, ControllerLeaseError> {
        if client_id.is_empty() || client_id.len() > 128 {
            return Err(ControllerLeaseError::Stale);
        }
        if self.holder.is_some() && !takeover {
            return Err(ControllerLeaseError::AlreadyHeld);
        }
        self.lease_id = self.lease_id.saturating_add(1).max(1);
        self.holder = Some(client_id.to_owned());
        Ok(self.lease_id)
    }

    pub fn validate(&self, client_id: &str, lease_id: u64) -> Result<(), ControllerLeaseError> {
        if self.holder.as_deref() == Some(client_id) && self.lease_id == lease_id {
            Ok(())
        } else {
            Err(ControllerLeaseError::Stale)
        }
    }

    pub fn release(&mut self, client_id: &str, lease_id: u64) -> Result<(), ControllerLeaseError> {
        self.validate(client_id, lease_id)?;
        self.holder = None;
        Ok(())
    }
}

pub fn decode_control(value: &Value) -> Result<TerminalCommand, TerminalCommandError> {
    let object = value.as_object().ok_or(TerminalCommandError::NotObject)?;
    if object.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err(TerminalCommandError::SchemaVersion);
    }
    if object.keys().any(|key| {
        !matches!(
            key.as_str(),
            "schema_version"
                | "request_id"
                | "session_id"
                | "terminal_id"
                | "terminal_epoch"
                | "lease_id"
                | "command"
                | "since_seq"
                | "input_b64"
                | "cols"
                | "rows"
                | "ack_seq"
        )
    }) {
        return Err(TerminalCommandError::Field("unknown"));
    }
    require_hex(object, "request_id", 32)?;
    require_string(object, "session_id")?;
    require_hex(object, "terminal_id", 32)?;
    let command = object
        .get("command")
        .and_then(Value::as_str)
        .ok_or(TerminalCommandError::Field("command"))?;
    match command {
        "attach" => Ok(TerminalCommand::Attach {
            since_seq: require_u64(object, "since_seq")?,
        }),
        "input" => Ok(TerminalCommand::Input {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
            lease_id: require_hex(object, "lease_id", 32)?.to_owned(),
            bytes: decode_input(object)?,
        }),
        "resize" => Ok(TerminalCommand::Resize {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
            lease_id: require_hex(object, "lease_id", 32)?.to_owned(),
            cols: dimension(object, "cols")?,
            rows: dimension(object, "rows")?,
        }),
        "interrupt" => Ok(TerminalCommand::Interrupt {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
        }),
        "terminate" => Ok(TerminalCommand::Terminate {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
        }),
        "ack" => Ok(TerminalCommand::Ack {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
            ack_seq: require_u64(object, "ack_seq")?,
        }),
        "takeover" => Ok(TerminalCommand::Takeover {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
        }),
        "detach" => Ok(TerminalCommand::Detach {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
        }),
        "close" => Ok(TerminalCommand::Close {
            epoch: require_hex(object, "terminal_epoch", 32)?.to_owned(),
        }),
        _ => Err(TerminalCommandError::Field("command")),
    }
}

fn require_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    key: &'static str,
) -> Result<&'a str, TerminalCommandError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .ok_or(TerminalCommandError::Field(key))
}

fn require_hex<'a>(
    object: &'a serde_json::Map<String, Value>,
    key: &'static str,
    len: usize,
) -> Result<&'a str, TerminalCommandError> {
    let value = require_string(object, key)?;
    if value.len() != len
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(TerminalCommandError::Field(key));
    }
    Ok(value)
}

fn require_u64(
    object: &serde_json::Map<String, Value>,
    key: &'static str,
) -> Result<u64, TerminalCommandError> {
    object
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(TerminalCommandError::Field(key))
}

fn dimension(
    object: &serde_json::Map<String, Value>,
    key: &'static str,
) -> Result<u16, TerminalCommandError> {
    let value = object
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(TerminalCommandError::Field(key))?;
    u16::try_from(value)
        .ok()
        .filter(|value| (1..=MAX_TERMINAL_DIMENSION).contains(value))
        .ok_or(TerminalCommandError::Dimensions)
}

fn decode_input(object: &serde_json::Map<String, Value>) -> Result<Vec<u8>, TerminalCommandError> {
    let encoded = require_string(object, "input_b64")?;
    let bytes = STANDARD
        .decode(encoded)
        .map_err(|_| TerminalCommandError::InvalidBase64)?;
    if bytes.len() > MAX_TERMINAL_INPUT_BYTES {
        return Err(TerminalCommandError::InputTooLarge);
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base(command: &str) -> Value {
        serde_json::json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "session_id": "session-1",
            "terminal_id": "b".repeat(32),
            "command": command,
        })
    }

    #[test]
    fn input_and_resize_are_decoded_with_bounds() {
        let mut input = base("input");
        input["terminal_epoch"] = Value::String("c".repeat(32));
        input["lease_id"] = Value::String("d".repeat(32));
        input["input_b64"] = Value::String("AQI=".into());
        assert_eq!(
            decode_control(&input),
            Ok(TerminalCommand::Input {
                epoch: "c".repeat(32),
                lease_id: "d".repeat(32),
                bytes: vec![1, 2]
            })
        );
        let mut resize = base("resize");
        resize["terminal_epoch"] = Value::String("c".repeat(32));
        resize["lease_id"] = Value::String("d".repeat(32));
        resize["cols"] = Value::from(120);
        resize["rows"] = Value::from(40);
        assert!(matches!(
            decode_control(&resize),
            Ok(TerminalCommand::Resize {
                cols: 120,
                rows: 40,
                ..
            })
        ));
    }

    #[test]
    fn malformed_identity_dimensions_and_input_fail_closed() {
        let mut attach = base("attach");
        attach["since_seq"] = Value::from(0);
        attach["request_id"] = Value::String("A".repeat(32));
        assert_eq!(
            decode_control(&attach),
            Err(TerminalCommandError::Field("request_id"))
        );
        let mut resize = base("resize");
        resize["terminal_epoch"] = Value::String("c".repeat(32));
        resize["lease_id"] = Value::String("d".repeat(32));
        resize["cols"] = Value::from(0);
        resize["rows"] = Value::from(40);
        assert_eq!(
            decode_control(&resize),
            Err(TerminalCommandError::Dimensions)
        );
        let mut input = base("input");
        input["terminal_epoch"] = Value::String("c".repeat(32));
        input["lease_id"] = Value::String("d".repeat(32));
        input["input_b64"] = Value::String("not-base64".into());
        assert_eq!(
            decode_control(&input),
            Err(TerminalCommandError::InvalidBase64)
        );
        let mut forged = base("attach");
        forged["since_seq"] = Value::from(0);
        forged["credential"] = Value::String("secret".into());
        assert_eq!(
            decode_control(&forged),
            Err(TerminalCommandError::Field("unknown"))
        );
    }

    #[test]
    fn controller_lease_allows_one_writer_and_explicit_takeover() {
        let mut lease = ControllerLease::default();
        let first = lease.acquire("browser-a", false).expect("first controller");
        assert_eq!(
            lease.acquire("browser-b", false),
            Err(ControllerLeaseError::AlreadyHeld)
        );
        assert!(lease.validate("browser-a", first).is_ok());
        let second = lease.acquire("browser-b", true).expect("takeover");
        assert_ne!(first, second);
        assert_eq!(
            lease.validate("browser-a", first),
            Err(ControllerLeaseError::Stale)
        );
        lease.release("browser-b", second).expect("release");
        assert!(lease.holder().is_none());
    }
}
