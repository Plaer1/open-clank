//! Health/lifecycle foundation for the owned Open Clank supervisor.
//!
//! S01 deliberately exposes no turn or arbitrary-process RPC.  The ACP
//! adapter remains the production backend until later slices prove semantic
//! and authority parity.

#[cfg(feature = "tonic-transport")]
pub mod protocol {
    tonic::include_proto!("openclank.agent_supervisor.v1");
}

pub mod instance_registry;
pub mod history_capture;
#[cfg(feature = "tonic-transport")]
pub mod server;
pub use instance_registry::RuntimeRootGuard;
pub mod bootstrap;
pub mod driver;
pub mod driver_control;
pub mod driver_process;
pub mod driver_session;
pub mod driver_wire;
pub mod process;
pub mod runtime_actor;
pub mod semantic;
pub mod terminal_control;
pub mod terminal_frame;
pub mod terminal_ring;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use thiserror::Error;

pub const PROTOCOL_MAJOR: &str = "1";

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum ReadinessPhase {
    Transport,
    Protocol,
    Containment,
    RuntimeActor,
    Driver,
    ToolPolicy,
    Ready,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Readiness {
    pub transport: bool,
    pub protocol: bool,
    pub containment: bool,
    pub runtime_actor: bool,
    pub driver: bool,
    pub tool_policy: bool,
    pub ready: bool,
}

impl Readiness {
    pub fn health_only() -> Self {
        Self {
            transport: true,
            protocol: true,
            containment: false,
            runtime_actor: false,
            driver: false,
            tool_policy: false,
            ready: false,
        }
    }
}

#[derive(Debug, Error)]
pub enum SupervisorError {
    #[error("runtime root is already in use")]
    RuntimeRootInUse,
    #[error("runtime root must be an absolute path")]
    RelativeRuntimeRoot,
    #[error("runtime root has unsafe permissions")]
    UnsafeRuntimeRoot,
    #[error("runtime root I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

pub fn build_id() -> &'static str {
    option_env!("OPENCLANK_AGENT_SUPERVISOR_BUILD_ID").unwrap_or(env!("CARGO_PKG_VERSION"))
}

pub fn schema_sha256() -> String {
    let mut digest = Sha256::new();
    digest.update(include_bytes!(
        "../proto/openclank_agent_supervisor_v1.proto"
    ));
    format!("{:x}", digest.finalize())
}

pub fn validate_runtime_root(path: &Path) -> Result<PathBuf, SupervisorError> {
    if !path.is_absolute() {
        return Err(SupervisorError::RelativeRuntimeRoot);
    }
    Ok(path.to_path_buf())
}

pub fn health_payload() -> serde_json::Value {
    let readiness = Readiness::health_only();
    serde_json::json!({
        "protocol_major": PROTOCOL_MAJOR,
        "build_id": build_id(),
        "schema_sha256": schema_sha256(),
        "transport": readiness.transport,
        "protocol": readiness.protocol,
        "containment": readiness.containment,
        "runtime_actor": readiness.runtime_actor,
        "driver": readiness.driver,
        "tool_policy": readiness.tool_policy,
        "ready": readiness.ready,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_does_not_claim_turn_readiness() {
        let readiness = Readiness::health_only();
        assert!(readiness.transport);
        assert!(readiness.protocol);
        assert!(!readiness.driver);
        assert!(!readiness.ready);
    }

    #[test]
    fn runtime_root_must_be_absolute() {
        assert!(matches!(
            validate_runtime_root(Path::new("relative")),
            Err(SupervisorError::RelativeRuntimeRoot)
        ));
    }

    #[test]
    fn schema_hash_is_stable_shape() {
        assert_eq!(schema_sha256().len(), 64);
    }
}
