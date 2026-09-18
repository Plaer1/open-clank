use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread::{self, JoinHandle};
use thiserror::Error;

use crate::driver_wire::{decode_response, encode, DriverEnvelope, DriverResponse};
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};

#[cfg(unix)]
use nix::sys::signal::{killpg, Signal};
#[cfg(unix)]
use nix::unistd::Pid;
#[cfg(unix)]
use std::os::unix::io::{AsRawFd, FromRawFd, RawFd};
#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ProcessSpecError {
    #[error("process program is empty")]
    EmptyProgram,
    #[error("process argument contains a NUL byte")]
    NulArgument,
    #[error("process cwd must be absolute")]
    RelativeCwd,
    #[error("process environment key is not allowed")]
    EnvironmentKeyNotAllowed,
    #[error("process environment contains a NUL byte")]
    NulEnvironment,
    #[error("process shebang interpreter is unavailable")]
    BadShebang,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ProcessIdentityError {
    #[error("process start identity is unavailable on this platform")]
    Unsupported,
    #[error("process start identity could not be read: {0}")]
    Io(String),
    #[error("process start identity data is malformed")]
    Malformed,
}

/// Return an OS-derived birth token suitable for binding a callback to one
/// process incarnation. PID alone is never sufficient because it can be
/// reused. The token intentionally contains no command, cwd, or user data.
#[cfg(unix)]
pub fn process_start_token(pid: u32) -> Result<String, ProcessIdentityError> {
    #[cfg(target_os = "macos")]
    {
        let mut info = unsafe { std::mem::zeroed::<nix::libc::proc_bsdinfo>() };
        let size = unsafe {
            nix::libc::proc_pidinfo(
                pid as nix::libc::c_int,
                nix::libc::PROC_PIDTBSDINFO,
                0,
                (&mut info as *mut nix::libc::proc_bsdinfo).cast(),
                std::mem::size_of::<nix::libc::proc_bsdinfo>() as nix::libc::c_int,
            )
        };
        if size != std::mem::size_of::<nix::libc::proc_bsdinfo>() as nix::libc::c_int {
            return Err(ProcessIdentityError::Io(
                std::io::Error::last_os_error().to_string(),
            ));
        }
        return Ok(format!(
            "macos:{}:{}",
            info.pbi_start_tvsec, info.pbi_start_tvusec
        ));
    }

    #[cfg(target_os = "linux")]
    {
        let stat = fs::read_to_string(format!("/proc/{pid}/stat"))
            .map_err(|error| ProcessIdentityError::Io(error.to_string()))?;
        let after_comm = stat
            .rsplit_once(") ")
            .ok_or(ProcessIdentityError::Malformed)?
            .1;
        let fields: Vec<&str> = after_comm.split_whitespace().collect();
        // After the comm field, index 19 is field 22 (starttime).
        let start_ticks = fields.get(19).ok_or(ProcessIdentityError::Malformed)?;
        let boot_id = fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .map_err(|error| ProcessIdentityError::Io(error.to_string()))?
            .trim()
            .to_owned();
        if boot_id.is_empty() {
            return Err(ProcessIdentityError::Malformed);
        }
        return Ok(format!("linux:{boot_id}:{start_ticks}"));
    }

    #[allow(unreachable_code)]
    Err(ProcessIdentityError::Unsupported)
}

#[cfg(not(unix))]
pub fn process_start_token(_pid: u32) -> Result<String, ProcessIdentityError> {
    Err(ProcessIdentityError::Unsupported)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpawnSpec {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub environment: BTreeMap<String, String>,
}

impl SpawnSpec {
    pub fn validate(&self, allowed_environment: &[&str]) -> Result<(), ProcessSpecError> {
        if self.program.is_empty() {
            return Err(ProcessSpecError::EmptyProgram);
        }
        if self.program.contains('\0') || self.args.iter().any(|arg| arg.contains('\0')) {
            return Err(ProcessSpecError::NulArgument);
        }
        if !self.cwd.is_absolute() {
            return Err(ProcessSpecError::RelativeCwd);
        }
        for (key, value) in &self.environment {
            if !allowed_environment.iter().any(|allowed| *allowed == key) {
                return Err(ProcessSpecError::EnvironmentKeyNotAllowed);
            }
            if key.contains('\0') || value.contains('\0') {
                return Err(ProcessSpecError::NulEnvironment);
            }
        }
        validate_program_shebang(&self.program)?;
        Ok(())
    }

    pub fn command_line(&self) -> Vec<&str> {
        std::iter::once(self.program.as_str())
            .chain(self.args.iter().map(String::as_str))
            .collect()
    }
}

fn validate_program_shebang(program: &str) -> Result<(), ProcessSpecError> {
    let path = Path::new(program);
    if !path.is_absolute() {
        return Ok(());
    }
    let Ok(bytes) = fs::read(path) else {
        return Ok(());
    };
    if !bytes.starts_with(b"#!") {
        return Ok(());
    }
    let line_end = bytes[2..]
        .iter()
        .position(|byte| *byte == b'\n' || *byte == b'\r')
        .map(|index| index + 2)
        .unwrap_or(bytes.len());
    let line = String::from_utf8_lossy(&bytes[2..line_end]);
    let mut parts = line.split_whitespace();
    let Some(interpreter) = parts.next() else {
        return Err(ProcessSpecError::BadShebang);
    };
    let resolved = if interpreter == "/usr/bin/env" || interpreter.ends_with("/env") {
        parts.next().unwrap_or_default()
    } else {
        interpreter
    };
    if resolved.starts_with('/') && !Path::new(resolved).is_file() {
        return Err(ProcessSpecError::BadShebang);
    }
    Ok(())
}

pub fn is_safe_cwd(path: &Path) -> bool {
    path.is_absolute()
}

#[derive(Debug, Error)]
pub enum PtySpawnError {
    #[error("invalid process specification: {0}")]
    Invalid(#[from] ProcessSpecError),
    #[error("PTY setup failed: {0}")]
    Setup(String),
}

#[derive(Debug)]
pub struct PtyOutputReader {
    receiver: Receiver<Result<Vec<u8>, String>>,
    join: Option<JoinHandle<()>>,
}

impl PtyOutputReader {
    pub fn recv(&self) -> Option<Result<Vec<u8>, String>> {
        self.receiver.recv().ok()
    }

    pub fn try_recv(&self) -> Option<Result<Vec<u8>, String>> {
        self.receiver.try_recv().ok()
    }

    pub fn join(mut self) -> Result<(), String> {
        self.join
            .take()
            .ok_or_else(|| "PTY reader thread already joined".to_owned())?
            .join()
            .map_err(|_| "PTY reader thread panicked".to_owned())
    }
}

pub struct PtySession {
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
}

/// PTY-backed driver process with one additional inherited fd 3 for the
/// structured driver protocol. The descriptor is installed only for the
/// child; the parent restores its own fd 3 immediately after spawn. PTY
/// reader/writer access remains separate from control framing.
#[cfg(unix)]
pub struct PtyControlProcess {
    master: fs::File,
    child: std::process::Child,
    control: UnixStream,
}

#[cfg(unix)]
impl PtyControlProcess {
    pub fn spawn(
        spec: &SpawnSpec,
        allowed_environment: &[&str],
        rows: u16,
        cols: u16,
    ) -> Result<Self, PtySpawnError> {
        spec.validate(allowed_environment)?;
        let mut master_fd = -1;
        let mut slave_fd = -1;
        if unsafe {
            nix::libc::openpty(
                &mut master_fd,
                &mut slave_fd,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        } != 0
        {
            return Err(PtySpawnError::Setup(
                std::io::Error::last_os_error().to_string(),
            ));
        }
        let winsize = nix::libc::winsize {
            ws_row: rows.max(1),
            ws_col: cols.max(1),
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        let _ = unsafe {
            nix::libc::ioctl(
                master_fd,
                nix::libc::TIOCSWINSZ,
                &winsize as *const nix::libc::winsize,
            )
        };
        let master = unsafe { fs::File::from_raw_fd(master_fd) };
        let (parent_control, child_control) =
            UnixStream::pair().map_err(|error| PtySpawnError::Setup(error.to_string()))?;
        let child_fd = child_control.as_raw_fd();

        let mut command = std::process::Command::new(&spec.program);
        command.args(&spec.args);
        command.current_dir(&spec.cwd);
        command.env_clear();
        for (key, value) in &spec.environment {
            command.env(key, value);
        }
        let stdin_fd = dup_fd(slave_fd).map_err(|error| {
            unsafe { nix::libc::close(slave_fd) };
            PtySpawnError::Setup(error.to_string())
        })?;
        let stdout_fd = dup_fd(slave_fd).map_err(|error| {
            unsafe { nix::libc::close(stdin_fd) };
            unsafe { nix::libc::close(slave_fd) };
            PtySpawnError::Setup(error.to_string())
        })?;
        let stderr_fd = dup_fd(slave_fd).map_err(|error| {
            unsafe { nix::libc::close(stdin_fd) };
            unsafe { nix::libc::close(stdout_fd) };
            unsafe { nix::libc::close(slave_fd) };
            PtySpawnError::Setup(error.to_string())
        })?;
        command.stdin(unsafe { std::process::Stdio::from(fs::File::from_raw_fd(stdin_fd)) });
        command.stdout(unsafe { std::process::Stdio::from(fs::File::from_raw_fd(stdout_fd)) });
        command.stderr(unsafe { std::process::Stdio::from(fs::File::from_raw_fd(stderr_fd)) });
        let child_fd_for_exec = child_fd;
        unsafe {
            use std::os::unix::process::CommandExt;
            command.pre_exec(move || {
                if nix::libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                if nix::libc::ioctl(0, nix::libc::TIOCSCTTY as _, 0) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                if nix::libc::dup2(child_fd_for_exec, 3) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                let flags = nix::libc::fcntl(3, nix::libc::F_GETFD);
                if flags == -1
                    || nix::libc::fcntl(3, nix::libc::F_SETFD, flags & !nix::libc::FD_CLOEXEC) == -1
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                drop(child_control);
                unsafe { nix::libc::close(slave_fd) };
                return Err(PtySpawnError::Setup(error.to_string()));
            }
        };
        unsafe { nix::libc::close(slave_fd) };
        drop(child_control);
        Ok(Self {
            master,
            child,
            control: parent_control,
        })
    }

    pub fn control(&mut self) -> &mut UnixStream {
        &mut self.control
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn exchange(
        &mut self,
        envelope: &DriverEnvelope,
    ) -> Result<DriverResponse, crate::driver_control::DriverControlError> {
        use crate::driver_control::DriverControlError;
        let body = encode(envelope)?;
        write_control_frame(&mut self.control, &body)?;
        let response = read_control_frame(&mut self.control)?.ok_or(DriverControlError::Io(
            std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "driver exited before responding",
            ),
        ))?;
        let decoded = decode_response(&response)?;
        if decoded.request_id != envelope.request_id {
            return Err(DriverControlError::Wire(
                crate::driver_wire::DriverWireError::ResponseField("request_id"),
            ));
        }
        Ok(decoded)
    }

    pub fn reader(&self) -> Result<Box<dyn std::io::Read + Send>, PtySpawnError> {
        self.master
            .try_clone()
            .map(|file| Box::new(file) as Box<dyn std::io::Read + Send>)
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    pub fn writer(&self) -> Result<Box<dyn std::io::Write + Send>, PtySpawnError> {
        self.master
            .try_clone()
            .map(|file| Box::new(file) as Box<dyn std::io::Write + Send>)
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    pub fn wait(&mut self) -> Result<std::process::ExitStatus, PtySpawnError> {
        self.child
            .wait()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    pub fn kill_group(&mut self) -> Result<(), PtySpawnError> {
        killpg(Pid::from_raw(self.child.id() as i32), Signal::SIGKILL)
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }
}

#[cfg(unix)]
impl Drop for PtyControlProcess {
    fn drop(&mut self) {
        let _ = self.kill_group();
    }
}

#[cfg(unix)]
fn read_control_frame(
    reader: &mut UnixStream,
) -> Result<Option<Vec<u8>>, crate::driver_control::DriverControlError> {
    use crate::driver_control::DriverControlError;
    use crate::driver_wire::{DriverWireError, MAX_DRIVER_ENVELOPE_BYTES};
    let mut header = [0u8; 4];
    match std::io::Read::read(reader, &mut header[..1])? {
        0 => return Ok(None),
        1 => {}
        _ => unreachable!(),
    }
    std::io::Read::read_exact(reader, &mut header[1..]).map_err(|error| {
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            DriverControlError::Truncated
        } else {
            DriverControlError::Io(error)
        }
    })?;
    let length = u32::from_be_bytes(header) as usize;
    if length == 0 || length > MAX_DRIVER_ENVELOPE_BYTES {
        return Err(DriverControlError::Wire(DriverWireError::TooLarge));
    }
    let mut body = vec![0u8; length];
    std::io::Read::read_exact(reader, &mut body).map_err(|error| {
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            DriverControlError::Truncated
        } else {
            DriverControlError::Io(error)
        }
    })?;
    Ok(Some(body))
}

#[cfg(unix)]
fn write_control_frame(
    writer: &mut UnixStream,
    body: &[u8],
) -> Result<(), crate::driver_control::DriverControlError> {
    use crate::driver_control::DriverControlError;
    let length = u32::try_from(body.len())
        .map_err(|_| DriverControlError::Wire(crate::driver_wire::DriverWireError::TooLarge))?;
    std::io::Write::write_all(writer, &length.to_be_bytes())?;
    std::io::Write::write_all(writer, body)?;
    std::io::Write::flush(writer)?;
    Ok(())
}

#[cfg(unix)]
fn dup_fd(source: RawFd) -> Result<RawFd, nix::errno::Errno> {
    let fd = unsafe { nix::libc::dup(source) };
    if fd < 0 {
        Err(nix::errno::Errno::last())
    } else {
        Ok(fd)
    }
}

impl PtySession {
    pub fn spawn(
        spec: &SpawnSpec,
        allowed_environment: &[&str],
        rows: u16,
        cols: u16,
    ) -> Result<Self, PtySpawnError> {
        spec.validate(allowed_environment)?;
        let pty_system = native_pty_system();
        let pair = pty_system
            .openpty(PtySize {
                rows: rows.max(1),
                cols: cols.max(1),
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|error| PtySpawnError::Setup(error.to_string()))?;
        let mut command = CommandBuilder::new(&spec.program);
        command.args(&spec.args);
        command.cwd(&spec.cwd);
        command.env_clear();
        for (key, value) in &spec.environment {
            command.env(key, value);
        }
        let child = pair
            .slave
            .spawn_command(command)
            .map_err(|error| PtySpawnError::Setup(error.to_string()))?;
        drop(pair.slave);
        Ok(Self {
            master: pair.master,
            child,
        })
    }

    pub fn reader(&self) -> Result<Box<dyn std::io::Read + Send>, PtySpawnError> {
        self.master
            .try_clone_reader()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    pub fn writer(&self) -> Result<Box<dyn std::io::Write + Send>, PtySpawnError> {
        self.master
            .take_writer()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    /// Start one dedicated blocking reader with a bounded handoff queue. A
    /// later actor decides whether a full queue means backpressure, detach, or
    /// replay-gap handling; the reader itself never grows memory unboundedly.
    pub fn spawn_reader(
        &self,
        queue_frames: usize,
        chunk_bytes: usize,
    ) -> Result<PtyOutputReader, PtySpawnError> {
        if queue_frames == 0
            || chunk_bytes == 0
            || chunk_bytes > crate::terminal_ring::MAX_TERMINAL_FRAME_BYTES
        {
            return Err(PtySpawnError::Setup(
                "PTY reader bounds are invalid".to_owned(),
            ));
        }
        let mut reader = self.reader()?;
        let (sender, receiver) = mpsc::sync_channel(queue_frames);
        let join = thread::Builder::new()
            .name("openclank-pty-reader".to_owned())
            .spawn(move || read_pty_chunks(&mut *reader, sender, chunk_bytes))
            .map_err(|error| PtySpawnError::Setup(error.to_string()))?;
        Ok(PtyOutputReader {
            receiver,
            join: Some(join),
        })
    }

    pub fn wait(&mut self) -> Result<portable_pty::ExitStatus, PtySpawnError> {
        self.child
            .wait()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    #[cfg(unix)]
    pub fn kill(&mut self) -> Result<(), PtySpawnError> {
        self.signal_group(Signal::SIGKILL)
    }

    #[cfg(not(unix))]
    pub fn kill(&mut self) -> Result<(), PtySpawnError> {
        self.child
            .kill()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    /// Send the provider's normal interrupt to its PTY process group. This is
    /// deliberately separate from `kill`: Ctrl-C should retain terminal
    /// semantics while supervisor shutdown can escalate to TERM/KILL.
    #[cfg(unix)]
    pub fn interrupt(&mut self) -> Result<(), PtySpawnError> {
        self.signal_group(Signal::SIGINT)
    }

    /// Ask the provider process group to terminate after the grace period.
    #[cfg(unix)]
    pub fn terminate(&mut self) -> Result<(), PtySpawnError> {
        self.signal_group(Signal::SIGTERM)
    }

    #[cfg(unix)]
    fn signal_group(&mut self, signal: Signal) -> Result<(), PtySpawnError> {
        if let Some(leader) = self.master.process_group_leader() {
            killpg(Pid::from_raw(leader), signal)
                .map_err(|error| PtySpawnError::Setup(error.to_string()))
        } else {
            self.child
                .kill()
                .map_err(|error| PtySpawnError::Setup(error.to_string()))
        }
    }

    #[cfg(not(unix))]
    pub fn interrupt(&mut self) -> Result<(), PtySpawnError> {
        self.child
            .kill()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }

    #[cfg(not(unix))]
    pub fn terminate(&mut self) -> Result<(), PtySpawnError> {
        self.child
            .kill()
            .map_err(|error| PtySpawnError::Setup(error.to_string()))
    }
}

fn read_pty_chunks(
    reader: &mut dyn std::io::Read,
    sender: SyncSender<Result<Vec<u8>, String>>,
    chunk_bytes: usize,
) {
    loop {
        let mut chunk = vec![0; chunk_bytes];
        match reader.read(&mut chunk) {
            Ok(0) => return,
            Ok(size) => {
                chunk.truncate(size);
                if sender.send(Ok(chunk)).is_err() {
                    return;
                }
            }
            Err(error) => {
                let _ = sender.send(Err(error.to_string()));
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    fn spec() -> SpawnSpec {
        SpawnSpec {
            program: "/usr/bin/provider".to_owned(),
            args: vec!["--structured".to_owned()],
            cwd: PathBuf::from("/workspace"),
            environment: BTreeMap::from([("TERM".to_owned(), "xterm-256color".to_owned())]),
        }
    }

    #[test]
    fn argv_and_allowlisted_environment_are_explicit() {
        let value = spec();
        value.validate(&["TERM"]).expect("valid spec");
        assert_eq!(
            value.command_line(),
            vec!["/usr/bin/provider", "--structured"]
        );
    }

    #[test]
    fn shell_and_secret_environment_do_not_cross_the_boundary() {
        let mut value = spec();
        value
            .environment
            .insert("OPENAI_API_KEY".to_owned(), "secret".to_owned());
        assert_eq!(
            value.validate(&["TERM"]),
            Err(ProcessSpecError::EnvironmentKeyNotAllowed)
        );
        value.environment.clear();
        value.args = vec!["-c".to_owned(), "provider".to_owned()];
        value
            .validate(&["TERM"])
            .expect("argv remains data, not a shell");
    }

    #[test]
    fn invalid_paths_and_nul_values_fail_closed() {
        let mut value = spec();
        value.cwd = PathBuf::from("relative");
        assert_eq!(
            value.validate(&["TERM"]),
            Err(ProcessSpecError::RelativeCwd)
        );
        value.cwd = PathBuf::from("/workspace");
        value.args = vec!["bad\0arg".to_owned()];
        assert_eq!(
            value.validate(&["TERM"]),
            Err(ProcessSpecError::NulArgument)
        );
    }

    #[cfg(unix)]
    #[test]
    fn missing_shebang_interpreter_fails_before_child_spawn() {
        use std::os::unix::fs::PermissionsExt;
        let directory = tempfile::tempdir().expect("tempdir");
        let program = directory.path().join("bad-shebang");
        fs::write(
            &program,
            b"#!/definitely/missing/openclank-interpreter\nprintf",
        )
        .expect("script");
        fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).expect("executable");
        let value = SpawnSpec {
            program: program.to_string_lossy().into_owned(),
            args: Vec::new(),
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        assert_eq!(value.validate(&[]), Err(ProcessSpecError::BadShebang));
    }

    #[cfg(unix)]
    #[test]
    fn pty_spawn_uses_argv_and_reaps_child() {
        let value = SpawnSpec {
            program: "/usr/bin/printf".to_owned(),
            args: vec!["pty-ok".to_owned()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let mut session = PtySession::spawn(&value, &[], 24, 80).expect("spawn");
        let mut output = Vec::new();
        session
            .reader()
            .expect("reader")
            .read_to_end(&mut output)
            .expect("read");
        session.wait().expect("wait");
        assert!(String::from_utf8_lossy(&output).contains("pty-ok"));
    }

    #[cfg(unix)]
    #[test]
    fn terminate_targets_the_provider_process_group() {
        let value = SpawnSpec {
            program: "/bin/sh".to_owned(),
            args: vec!["-c".to_owned(), "sleep 30".to_owned()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let mut session = PtySession::spawn(&value, &[], 24, 80).expect("spawn");
        assert!(session.master.process_group_leader().is_some());
        session.terminate().expect("terminate process group");
        session.wait().expect("reap process group leader");
    }

    #[cfg(unix)]
    #[test]
    fn bad_executable_is_reported_at_spawn_boundary() {
        let value = SpawnSpec {
            program: "/definitely/missing/openclank-provider".to_owned(),
            args: Vec::new(),
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        assert!(PtySession::spawn(&value, &[], 24, 80).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn bounded_reader_delivers_chunks_and_can_be_joined() {
        let value = SpawnSpec {
            program: "/usr/bin/printf".to_owned(),
            args: vec!["reader-ok".to_owned()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let mut session = PtySession::spawn(&value, &[], 24, 80).expect("spawn");
        let reader = session.spawn_reader(2, 3).expect("bounded reader");
        let mut output = Vec::new();
        while let Some(chunk) = reader.recv() {
            output.extend(chunk.expect("reader chunk"));
        }
        reader.join().expect("reader join");
        session.wait().expect("wait");
        assert_eq!(output, b"reader-ok");
    }

    #[cfg(unix)]
    #[test]
    fn process_start_token_is_not_just_a_reusable_pid() {
        let token = process_start_token(std::process::id()).expect("current process identity");
        assert!(!token.is_empty());
        assert!(token.starts_with("macos:") || token.starts_with("linux:"));
        assert_ne!(token, std::process::id().to_string());
    }

    #[cfg(unix)]
    #[test]
    fn pty_control_process_keeps_structured_fd_separate_from_terminal_bytes() {
        let python = ["/usr/bin/python3", "/opt/homebrew/bin/python3"]
            .into_iter()
            .find(|path| Path::new(path).is_file());
        let Some(python) = python else {
            return;
        };
        let script = "import os; os.write(3, b'control-ok'); os.write(1, b'pty-ok\\n')";
        let value = SpawnSpec {
            program: python.to_owned(),
            args: vec!["-c".into(), script.into()],
            cwd: PathBuf::from("/tmp"),
            environment: BTreeMap::new(),
        };
        let mut process = PtyControlProcess::spawn(&value, &[], 24, 80).expect("spawn");
        let mut control = [0u8; 10];
        process
            .control()
            .read_exact(&mut control)
            .expect("control bytes");
        assert_eq!(&control, b"control-ok");
        let mut terminal = Vec::new();
        process
            .reader()
            .expect("pty reader")
            .read_to_end(&mut terminal)
            .expect("terminal bytes");
        process.wait().expect("reap");
        assert!(String::from_utf8_lossy(&terminal).contains("pty-ok"));
        assert!(!String::from_utf8_lossy(&terminal).contains("control-ok"));
    }
}
