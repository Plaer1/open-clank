use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DriverMode {
    Chat,
    Plan,
    Agent,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DriverCapabilities {
    pub semantic_transcript: bool,
    pub typed_tools: bool,
    pub native_tools_disabled: bool,
    pub host_tool_broker: bool,
    pub permissions: bool,
    pub questions: bool,
    pub plans: bool,
    pub models: bool,
    pub usage: bool,
    pub subagents: bool,
    pub resume: bool,
    pub memory_digest: bool,
    pub memory_tools: bool,
    pub memory_capture: bool,
    pub skills: bool,
    pub pty: bool,
    pub terminal_input: bool,
    pub secret_lane: bool,
    pub provider_control: bool,
    pub managed_operations: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriverDescriptor {
    pub adapter_id: String,
    pub protocol_major: u16,
    pub protocol_minor: u16,
    pub build_id: String,
    pub capabilities: DriverCapabilities,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DriverAdmissionError {
    #[error("driver adapter identity is missing")]
    MissingIdentity,
    #[error("driver protocol major is incompatible")]
    IncompatibleProtocol,
    #[error("driver lacks semantic transcript capability")]
    SemanticTranscriptMissing,
    #[error("driver cannot prove native side effects are disabled")]
    NativeToolsNotDisabled,
    #[error("driver lacks host tool broker capability")]
    HostToolBrokerMissing,
    #[error("driver lacks typed tool capability")]
    TypedToolsMissing,
    #[error("driver lacks typed plan capability")]
    PlansMissing,
}

impl DriverDescriptor {
    pub fn validate(&self, expected_major: u16) -> Result<(), DriverAdmissionError> {
        if self.adapter_id.is_empty() || self.adapter_id.len() > 128 || self.build_id.is_empty() {
            return Err(DriverAdmissionError::MissingIdentity);
        }
        if self.protocol_major != expected_major {
            return Err(DriverAdmissionError::IncompatibleProtocol);
        }
        Ok(())
    }

    pub fn admit_mode(&self, mode: DriverMode) -> Result<(), DriverAdmissionError> {
        self.validate(1)?;
        let capabilities = &self.capabilities;
        if !capabilities.semantic_transcript {
            return Err(DriverAdmissionError::SemanticTranscriptMissing);
        }
        if !capabilities.native_tools_disabled {
            return Err(DriverAdmissionError::NativeToolsNotDisabled);
        }
        match mode {
            DriverMode::Chat => Ok(()),
            DriverMode::Plan => {
                if !capabilities.host_tool_broker {
                    return Err(DriverAdmissionError::HostToolBrokerMissing);
                }
                if !capabilities.plans {
                    return Err(DriverAdmissionError::PlansMissing);
                }
                Ok(())
            }
            DriverMode::Agent => {
                if !capabilities.typed_tools {
                    return Err(DriverAdmissionError::TypedToolsMissing);
                }
                if !capabilities.host_tool_broker {
                    return Err(DriverAdmissionError::HostToolBrokerMissing);
                }
                Ok(())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn descriptor() -> DriverDescriptor {
        DriverDescriptor {
            adapter_id: "openclank-direct-v1".into(),
            protocol_major: 1,
            protocol_minor: 0,
            build_id: "fixture-build".into(),
            capabilities: DriverCapabilities {
                semantic_transcript: true,
                typed_tools: true,
                native_tools_disabled: true,
                host_tool_broker: true,
                plans: true,
                ..Default::default()
            },
        }
    }

    #[test]
    fn capability_matrix_admits_only_proven_modes() {
        let descriptor = descriptor();
        assert!(descriptor.admit_mode(DriverMode::Chat).is_ok());
        assert!(descriptor.admit_mode(DriverMode::Plan).is_ok());
        assert!(descriptor.admit_mode(DriverMode::Agent).is_ok());
        let mut degraded = descriptor.clone();
        degraded.capabilities.native_tools_disabled = false;
        assert_eq!(
            degraded.admit_mode(DriverMode::Agent),
            Err(DriverAdmissionError::NativeToolsNotDisabled)
        );
    }

    #[test]
    fn raw_pty_like_descriptor_is_not_tool_authority() {
        let mut descriptor = descriptor();
        descriptor.capabilities.semantic_transcript = false;
        descriptor.capabilities.typed_tools = false;
        descriptor.capabilities.host_tool_broker = false;
        descriptor.capabilities.plans = false;
        descriptor.capabilities.pty = true;
        assert_eq!(
            descriptor.admit_mode(DriverMode::Chat),
            Err(DriverAdmissionError::SemanticTranscriptMissing)
        );
    }

    #[test]
    fn identity_and_protocol_mismatches_fail_closed() {
        let mut missing = descriptor();
        missing.adapter_id.clear();
        assert_eq!(
            missing.validate(1),
            Err(DriverAdmissionError::MissingIdentity)
        );
        let mut incompatible = descriptor();
        incompatible.protocol_major = 2;
        assert_eq!(
            incompatible.validate(1),
            Err(DriverAdmissionError::IncompatibleProtocol)
        );
    }
}
