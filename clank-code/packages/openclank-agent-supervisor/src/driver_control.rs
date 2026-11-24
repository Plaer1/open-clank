use crate::driver_session::DriverSession;
use crate::driver_wire::{
    decode, decode_response, encode, encode_response, DriverEnvelope, DriverResponse,
    DriverWireError, MAX_DRIVER_ENVELOPE_BYTES,
};
use std::io::{self, Read, Write};
use thiserror::Error;

#[cfg(unix)]
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;
#[cfg(unix)]
use std::os::unix::process::CommandExt;

#[cfg(unix)]
use nix::sys::signal::{killpg, Signal};
#[cfg(unix)]
use nix::unistd::Pid;
#[cfg(unix)]
use std::process::{Child, Command, Stdio};

#[derive(Debug, Error)]
pub enum DriverControlError {
    #[error("driver control I/O failed: {0}")]
    Io(#[from] io::Error),
    #[error("driver control frame rejected: {0}")]
    Wire(#[from] DriverWireError),
    #[error("driver control frame is truncated")]
    Truncated,
    #[error("driver control process is unsupported on this platform")]
    UnsupportedPlatform,
    #[error("driver control process spawn failed: {0}")]
    Spawn(String),
}

#[cfg(unix)]
pub struct DriverControlProcess {
    child: Child,
    control: UnixStream,
}

#[cfg(unix)]
impl DriverControlProcess {
    /// Spawn a control-only driver with fd 3 bound to one private socket end.
    /// PTY attachment and descendant containment are deliberately separate
    /// admission work; this helper proves only the inherited control seam.
    pub fn spawn(
        spec: &crate::process::SpawnSpec,
        allowed_environment: &[&str],
    ) -> Result<Self, DriverControlError> {
        spec.validate(allowed_environment)
            .map_err(|error| DriverControlError::Spawn(error.to_string()))?;
        let (parent, child_end) = UnixStream::pair()?;
        let child_fd = child_end.as_raw_fd();
        let mut command = Command::new(&spec.program);
        command.args(&spec.args);
        command.current_dir(&spec.cwd);
        command.env_clear();
        for (key, value) in &spec.environment {
            command.env(key, value);
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            command.pre_exec(move || {
                if nix::libc::setsid() == -1 {
                    return Err(io::Error::last_os_error());
                }
                if nix::libc::dup2(child_fd, 3) == -1 {
                    return Err(io::Error::last_os_error());
                }
                if nix::libc::fcntl(3, nix::libc::F_SETFD, 0) == -1 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = command
            .spawn()
            .map_err(|error| DriverControlError::Spawn(error.to_string()))?;
        drop(child_end);
        Ok(Self {
            child,
            control: parent,
        })
    }

    pub fn control(&mut self) -> &mut UnixStream {
        &mut self.control
    }

    /// Send exactly one validated request and read exactly one response on the
    /// private inherited descriptor. This is intentionally synchronous: the
    /// runtime actor owns one control stream, so concurrent callers must be
    /// serialized by that actor rather than racing frames on the wire.
    pub fn exchange(
        &mut self,
        envelope: &DriverEnvelope,
    ) -> Result<DriverResponse, DriverControlError> {
        let body = encode(envelope)?;
        write_frame(&mut self.control, &body)?;
        let response =
            read_frame(&mut self.control)?.ok_or(DriverControlError::Io(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "driver exited before responding",
            )))?;
        let decoded = decode_response(&response)?;
        if decoded.request_id != envelope.request_id {
            return Err(DriverControlError::Wire(DriverWireError::ResponseField(
                "request_id",
            )));
        }
        Ok(decoded)
    }

    pub fn wait(&mut self) -> Result<std::process::ExitStatus, DriverControlError> {
        self.child
            .wait()
            .map_err(|error| DriverControlError::Spawn(error.to_string()))
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn start_token(&self) -> Result<String, DriverControlError> {
        crate::process::process_start_token(self.pid())
            .map_err(|error| DriverControlError::Spawn(error.to_string()))
    }

    pub fn kill(&mut self) -> Result<(), DriverControlError> {
        self.child
            .kill()
            .map_err(|error| DriverControlError::Spawn(error.to_string()))
    }

    pub fn kill_group(&mut self) -> Result<(), DriverControlError> {
        if self
            .child
            .try_wait()
            .map_err(|error| DriverControlError::Spawn(error.to_string()))?
            .is_some()
        {
            return Ok(());
        }
        killpg(Pid::from_raw(self.child.id() as i32), Signal::SIGKILL)
            .map_err(|error| DriverControlError::Spawn(error.to_string()))
    }
}

#[cfg(not(unix))]
pub struct DriverControlProcess;

#[cfg(not(unix))]
impl DriverControlProcess {
    pub fn spawn(
        _spec: &crate::process::SpawnSpec,
        _allowed_environment: &[&str],
    ) -> Result<Self, DriverControlError> {
        Err(DriverControlError::UnsupportedPlatform)
    }
}

/// Run one bounded length-delimited control stream until EOF or shutdown.
///
/// The caller owns the full-duplex descriptor and process identity. This
/// helper only frames/validates messages and delegates lifecycle authority to
/// `DriverSession`; it never reads PTY bytes or provider credentials.
pub fn run<R: Read, W: Write>(reader: &mut R, writer: &mut W) -> Result<(), DriverControlError> {
    let mut session = DriverSession::default();
    loop {
        let Some(frame) = read_frame(reader)? else {
            return Ok(());
        };
        let envelope = decode(&frame)?;
        let shutdown = matches!(
            envelope.command,
            crate::driver_wire::DriverCommand::Shutdown
        );
        let response = session.handle(envelope);
        let encoded = encode_response(&response)?;
        write_frame(writer, &encoded)?;
        if shutdown {
            return Ok(());
        }
    }
}

fn read_frame<R: Read>(reader: &mut R) -> Result<Option<Vec<u8>>, DriverControlError> {
    let mut header = [0u8; 4];
    match reader.read(&mut header[..1])? {
        0 => return Ok(None),
        1 => {}
        _ => unreachable!(),
    }
    reader.read_exact(&mut header[1..]).map_err(|error| {
        if error.kind() == io::ErrorKind::UnexpectedEof {
            DriverControlError::Truncated
        } else {
            DriverControlError::Io(error)
        }
    })?;
    let length = u32::from_be_bytes(header) as usize;
    if length == 0 || length > MAX_DRIVER_ENVELOPE_BYTES {
        return Err(DriverWireError::TooLarge.into());
    }
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body).map_err(|error| {
        if error.kind() == io::ErrorKind::UnexpectedEof {
            DriverControlError::Truncated
        } else {
            DriverControlError::Io(error)
        }
    })?;
    Ok(Some(body))
}

fn write_frame<W: Write>(writer: &mut W, body: &[u8]) -> Result<(), DriverControlError> {
    let length = u32::try_from(body.len()).map_err(|_| DriverWireError::TooLarge)?;
    writer.write_all(&length.to_be_bytes())?;
    writer.write_all(body)?;
    writer.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::driver_wire::decode_response;
    use serde_json::json;

    fn frame(command: &str, payload: serde_json::Value) -> Vec<u8> {
        let body = serde_json::to_vec(&json!({
            "schema_version": 1,
            "request_id": "a".repeat(32),
            "owner_subject_id": "subject-1",
            "session_id": "session-1",
            "runtime_id": "runtime-1",
            "runtime_epoch": "b".repeat(32),
            "runtime_generation": 2,
            "command": command,
            "payload": payload
        }))
        .expect("json");
        let mut output = (body.len() as u32).to_be_bytes().to_vec();
        output.extend(body);
        output
    }

    #[test]
    fn control_loop_round_trips_activation_and_shutdown() {
        let activation = json!({
            "registration_id": "e".repeat(32),
            "callback_endpoint": "/tmp/callback.sock",
            "callback_nonce": "N".repeat(43),
            "callback_binding_sha256": "f".repeat(64)
        });
        let mut input = frame(
            "hello",
            json!({
                "protocol_major": 1,
                "protocol_minor": 0,
                "schema_sha256": "c".repeat(64),
                "binding": "d".repeat(64)
            }),
        );
        input.extend(frame("activate", activation));
        input.extend(frame("shutdown", json!({})));
        let mut output = Vec::new();
        run(&mut input.as_slice(), &mut output).expect("control loop");
        let mut cursor = output.as_slice();
        let mut events = Vec::new();
        while cursor.len() >= 4 {
            let length = u32::from_be_bytes(cursor[..4].try_into().unwrap()) as usize;
            let end = 4 + length;
            events.push(decode_response(&cursor[4..end]).expect("response").event);
            cursor = &cursor[end..];
        }
        assert_eq!(
            events,
            vec![
                Some("hello_ack".into()),
                Some("activated".into()),
                Some("shutdown_ack".into())
            ]
        );
    }

    #[test]
    fn control_loop_rejects_truncated_body_without_partial_response() {
        let input = vec![0, 0, 0, 12, b'{'];
        let mut output = Vec::new();
        assert!(matches!(
            run(&mut input.as_slice(), &mut output),
            Err(DriverControlError::Truncated)
        ));
        assert!(output.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn unix_driver_process_receives_only_the_explicit_control_descriptor() {
        use std::collections::BTreeMap;
        use std::path::PathBuf;

        let spec = crate::process::SpawnSpec {
            program: "/bin/sh".into(),
            args: vec!["-c".into(), "printf ok >&3".into()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let mut process = DriverControlProcess::spawn(&spec, &[]).expect("spawn");
        let mut output = [0u8; 2];
        process
            .control()
            .read_exact(&mut output)
            .expect("control read");
        assert_eq!(&output, b"ok");
        assert!(process.wait().expect("wait").success());
    }
}
