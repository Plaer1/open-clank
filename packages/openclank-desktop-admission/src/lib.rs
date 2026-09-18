use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;
use std::env;
use std::error::Error;
use std::fs;
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use url::Url;

pub const READY_PATH: &str = "/api/desktop-shell/ready";
pub const CHALLENGE_HEADER: &str = "X-Open-Clank-Desktop-Challenge";
const PROOF_DOMAIN: &str = "open-clank-desktop-shell-v1";
const MAX_HTTP_RESPONSE_BYTES: u64 = 64 * 1024;
const DEFAULT_READINESS_TIMEOUT: Duration = Duration::from_secs(120);

pub type ShellResult<T> = Result<T, Box<dyn Error + Send + Sync>>;

fn shell_error(message: impl Into<String>) -> Box<dyn Error + Send + Sync> {
    Box::new(io::Error::new(io::ErrorKind::Other, message.into()))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendMode {
    Managed,
    Attached,
}

#[derive(Debug)]
pub struct Backend {
    origin: Url,
    mode: BackendMode,
    auth_enabled: Option<bool>,
    child: Option<Child>,
}

impl Backend {
    pub fn prepare() -> ShellResult<Self> {
        if let Some(raw_origin) = nonempty_env("OPEN_CLANK_SHELL_ATTACH_ORIGIN") {
            let origin = exact_loopback_origin(&raw_origin)?;
            wait_for_attached_health(&origin, Duration::from_secs(10))?;
            eprintln!(
                "OPEN_CLANK_SHELL_EVENT backend-ready mode=attached origin={} native-api=disabled",
                origin
            );
            return Ok(Self {
                origin,
                mode: BackendMode::Attached,
                auth_enabled: None,
                child: None,
            });
        }

        let repo_root = resolve_repo_root()?;
        let python = resolve_python(&repo_root)?;
        let bootstrap = repo_root.join("scripts/openclank_bootstrap.py");
        if !bootstrap.is_file() {
            return Err(shell_error(format!(
                "Open Clank bootstrap is missing: {}",
                bootstrap.display()
            )));
        }

        let port = reserve_loopback_port()?;
        let origin = exact_loopback_origin(&format!("http://127.0.0.1:{port}"))?;
        let mut secret = [0_u8; 32];
        getrandom::fill(&mut secret)
            .map_err(|error| shell_error(format!("readiness nonce generation failed: {error}")))?;
        let nonce = hex::encode(secret);

        let mut command = Command::new(&python);
        command
            .current_dir(&repo_root)
            .arg(&bootstrap)
            .arg("serve")
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(port.to_string())
            .env("APP_BIND", "127.0.0.1")
            .env("APP_PORT", port.to_string())
            .env("LOCALHOST_BYPASS", "false")
            .env("OPEN_CLANK_DESKTOP_READINESS_NONCE", nonce)
            .env("OPEN_CLANK_DESKTOP_EXPECTED_ORIGIN", origin.as_str())
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .process_group(0);

        let mut child = command.spawn().map_err(|error| {
            shell_error(format!("failed to launch {}: {error}", bootstrap.display()))
        })?;
        let pid = child.id();
        eprintln!(
            "OPEN_CLANK_SHELL_EVENT backend-spawned pid={pid} origin={} python={}",
            origin,
            python.display()
        );

        let timeout = readiness_timeout()?;
        let auth_enabled = match wait_for_managed_readiness(&mut child, &origin, &secret, timeout) {
            Ok(auth_enabled) => auth_enabled,
            Err(error) => {
                let mut backend = Self {
                    origin,
                    mode: BackendMode::Managed,
                    auth_enabled: None,
                    child: Some(child),
                };
                let _ = backend.shutdown();
                return Err(error);
            }
        };
        if !auth_enabled {
            let mut backend = Self {
                origin,
                mode: BackendMode::Managed,
                auth_enabled: Some(false),
                child: Some(child),
            };
            let _ = backend.shutdown();
            return Err(shell_error(
                "managed desktop shell requires AUTH_ENABLED=true",
            ));
        }
        eprintln!(
            "OPEN_CLANK_SHELL_EVENT backend-ready mode=managed pid={pid} origin={} auth-enabled=true",
            origin
        );
        Ok(Self {
            origin,
            mode: BackendMode::Managed,
            auth_enabled: Some(true),
            child: Some(child),
        })
    }

    pub fn origin(&self) -> &Url {
        &self.origin
    }

    pub fn mode(&self) -> BackendMode {
        self.mode
    }

    pub fn auth_enabled(&self) -> Option<bool> {
        self.auth_enabled
    }

    pub fn child_pid(&self) -> Option<u32> {
        self.child.as_ref().map(Child::id)
    }

    pub fn shutdown(&mut self) -> ShellResult<Option<ExitStatus>> {
        let Some(mut child) = self.child.take() else {
            return Ok(None);
        };
        let pid = child.id();
        if let Some(status) = child.try_wait()? {
            eprintln!(
                "OPEN_CLANK_SHELL_EVENT backend-stopped pid={pid} status={status} already-exited=true"
            );
            return Ok(Some(status));
        }

        // The backend gets a bounded graceful shutdown window so its own
        // supervisors can reap descendants. The process group is force-killed
        // only if that contract fails.
        let signal_result = unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        if signal_result != 0 {
            return Err(Box::new(io::Error::last_os_error()));
        }
        let deadline = Instant::now() + Duration::from_secs(12);
        loop {
            if let Some(status) = child.try_wait()? {
                eprintln!(
                    "OPEN_CLANK_SHELL_EVENT backend-stopped pid={pid} status={status} graceful=true"
                );
                return Ok(Some(status));
            }
            if Instant::now() >= deadline {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }

        unsafe {
            libc::kill(-(pid as libc::pid_t), libc::SIGKILL);
        }
        let _ = child.kill();
        let status = child.wait()?;
        eprintln!(
            "OPEN_CLANK_SHELL_EVENT backend-stopped pid={pid} status={status} graceful=false"
        );
        Ok(Some(status))
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

fn nonempty_env(name: &str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn resolve_repo_root() -> ShellResult<PathBuf> {
    let configured = nonempty_env("OPEN_CLANK_SHELL_REPO")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .and_then(Path::parent)
                .expect("desktop admission package must live below packages/")
                .to_path_buf()
        });
    let canonical = fs::canonicalize(&configured).map_err(|error| {
        shell_error(format!(
            "Open Clank repository path is unavailable ({}): {error}",
            configured.display()
        ))
    })?;
    if !canonical.join("app.py").is_file() {
        return Err(shell_error(format!(
            "not an Open Clank repository: {}",
            canonical.display()
        )));
    }
    Ok(canonical)
}

fn resolve_python(repo_root: &Path) -> ShellResult<PathBuf> {
    let configured = nonempty_env("OPEN_CLANK_SHELL_PYTHON").map(PathBuf::from);
    let candidates = if let Some(path) = configured {
        vec![if path.is_absolute() {
            path
        } else {
            repo_root.join(path)
        }]
    } else {
        vec![
            repo_root.join("venv/bin/python"),
            repo_root.join(".venv/bin/python"),
        ]
    };
    for candidate in candidates {
        if candidate.is_file() {
            // Keep the venv entrypoint path. Canonicalizing its interpreter
            // symlink bypasses pyvenv.cfg and silently loses installed deps.
            return Ok(candidate);
        }
    }
    Err(shell_error(
        "no repository Python found; set OPEN_CLANK_SHELL_PYTHON",
    ))
}

fn reserve_loopback_port() -> ShellResult<u16> {
    let requested = match nonempty_env("OPEN_CLANK_SHELL_PORT") {
        Some(raw) => Some(
            raw.parse::<u16>()
                .map_err(|_| shell_error("OPEN_CLANK_SHELL_PORT must be between 1 and 65535"))?,
        ),
        None => None,
    };
    if requested == Some(0) {
        return Err(shell_error(
            "OPEN_CLANK_SHELL_PORT must be between 1 and 65535",
        ));
    }
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, requested.unwrap_or(0)))?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

fn readiness_timeout() -> ShellResult<Duration> {
    let Some(raw) = nonempty_env("OPEN_CLANK_SHELL_READY_TIMEOUT_SECONDS") else {
        return Ok(DEFAULT_READINESS_TIMEOUT);
    };
    let seconds = raw
        .parse::<u64>()
        .map_err(|_| shell_error("OPEN_CLANK_SHELL_READY_TIMEOUT_SECONDS must be an integer"))?;
    if !(5..=180).contains(&seconds) {
        return Err(shell_error(
            "OPEN_CLANK_SHELL_READY_TIMEOUT_SECONDS must be between 5 and 180",
        ));
    }
    Ok(Duration::from_secs(seconds))
}

pub fn exact_loopback_origin(raw: &str) -> ShellResult<Url> {
    let url = Url::parse(raw)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || !matches!(url.path(), "" | "/")
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(shell_error(
            "desktop shell origin must be exactly http://127.0.0.1:<port>",
        ));
    }
    Ok(Url::parse(&format!(
        "http://127.0.0.1:{}",
        url.port().expect("checked explicit port")
    ))?)
}

pub fn navigation_is_allowed(expected_origin: &Url, candidate: &Url) -> bool {
    candidate.scheme() == "http"
        && candidate.host_str() == Some("127.0.0.1")
        && candidate.port() == expected_origin.port()
        && candidate.username().is_empty()
        && candidate.password().is_none()
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadinessPayload {
    schema_version: u32,
    ready: bool,
    auth_enabled: bool,
    origin: String,
    pid: u32,
    proof: String,
}

fn proof_message(payload: &ReadinessPayload, challenge: &str) -> Vec<u8> {
    format!(
        "{PROOF_DOMAIN}\n{challenge}\n{}\n{}\n{}\n{}",
        payload.origin,
        payload.pid,
        u8::from(payload.ready),
        u8::from(payload.auth_enabled)
    )
    .into_bytes()
}

fn verify_readiness_payload(
    payload: &ReadinessPayload,
    status: u16,
    expected_origin: &Url,
    expected_pid: u32,
    secret: &[u8; 32],
    challenge: &str,
) -> ShellResult<()> {
    if payload.schema_version != 1
        || payload.origin != expected_origin.as_str().trim_end_matches('/')
        || payload.pid != expected_pid
        || status != if payload.ready { 200 } else { 503 }
    {
        return Err(shell_error("desktop readiness binding fields do not match"));
    }
    let proof = hex::decode(&payload.proof)
        .map_err(|_| shell_error("desktop readiness proof is not lowercase hex"))?;
    if payload.proof.len() != 64 || payload.proof != payload.proof.to_ascii_lowercase() {
        return Err(shell_error("desktop readiness proof is not lowercase hex"));
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(secret)
        .map_err(|_| shell_error("desktop readiness HMAC key is invalid"))?;
    mac.update(&proof_message(payload, challenge));
    mac.verify_slice(&proof)
        .map_err(|_| shell_error("desktop readiness proof did not verify"))
}

fn wait_for_managed_readiness(
    child: &mut Child,
    origin: &Url,
    secret: &[u8; 32],
    timeout: Duration,
) -> ShellResult<bool> {
    let deadline = Instant::now() + timeout;
    let expected_pid = child.id();
    let mut bound_child_seen = false;
    loop {
        if let Some(status) = child.try_wait()? {
            return Err(shell_error(format!(
                "Open Clank backend exited before readiness: {status}"
            )));
        }
        let mut challenge_bytes = [0_u8; 32];
        getrandom::fill(&mut challenge_bytes).map_err(|error| {
            shell_error(format!("readiness challenge generation failed: {error}"))
        })?;
        let challenge = hex::encode(challenge_bytes);
        match http_get(
            origin,
            READY_PATH,
            &[(CHALLENGE_HEADER, challenge.as_str())],
        ) {
            Ok(response) if matches!(response.status, 200 | 503) => {
                if let Ok(payload) = serde_json::from_slice::<ReadinessPayload>(&response.body) {
                    if verify_readiness_payload(
                        &payload,
                        response.status,
                        origin,
                        expected_pid,
                        secret,
                        &challenge,
                    )
                    .is_ok()
                    {
                        bound_child_seen = true;
                        if payload.ready {
                            return Ok(payload.auth_enabled);
                        }
                    }
                }
            }
            Ok(_) | Err(_) => {}
        }
        if Instant::now() >= deadline {
            return Err(shell_error(format!(
                "Open Clank backend readiness timed out (child-bound-response-seen={bound_child_seen})"
            )));
        }
        thread::sleep(Duration::from_millis(250));
    }
}

fn wait_for_attached_health(origin: &Url, timeout: Duration) -> ShellResult<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(response) = http_get(origin, "/api/health", &[]) {
            if response.status == 200 {
                let payload: serde_json::Value = serde_json::from_slice(&response.body)?;
                if payload.get("status").and_then(serde_json::Value::as_str) == Some("healthy") {
                    return Ok(());
                }
            }
        }
        if Instant::now() >= deadline {
            return Err(shell_error(
                "attached Open Clank origin did not become healthy",
            ));
        }
        thread::sleep(Duration::from_millis(250));
    }
}

#[derive(Debug)]
struct HttpResponse {
    status: u16,
    body: Vec<u8>,
}

fn http_get(origin: &Url, path: &str, headers: &[(&str, &str)]) -> ShellResult<HttpResponse> {
    let port = origin
        .port()
        .ok_or_else(|| shell_error("loopback origin has no explicit port"))?;
    let address = SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(750))?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n"
    )?;
    for (name, value) in headers {
        write!(stream, "{name}: {value}\r\n")?;
    }
    stream.write_all(b"\r\n")?;
    stream.flush()?;

    let mut bytes = Vec::new();
    stream
        .take(MAX_HTTP_RESPONSE_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_HTTP_RESPONSE_BYTES {
        return Err(shell_error("loopback HTTP response exceeded 64 KiB"));
    }
    parse_http_response(&bytes)
}

fn parse_http_response(bytes: &[u8]) -> ShellResult<HttpResponse> {
    let split = bytes
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| shell_error("loopback HTTP response has no header terminator"))?;
    let header = std::str::from_utf8(&bytes[..split])?;
    let mut lines = header.split("\r\n");
    let status_line = lines
        .next()
        .ok_or_else(|| shell_error("loopback HTTP response has no status"))?;
    let mut status_parts = status_line.split_whitespace();
    if status_parts.next() != Some("HTTP/1.1") {
        return Err(shell_error("loopback HTTP response is not HTTP/1.1"));
    }
    let status = status_parts
        .next()
        .ok_or_else(|| shell_error("loopback HTTP response has no status code"))?
        .parse::<u16>()?;
    let mut content_length = None;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return Err(shell_error("loopback HTTP response has a malformed header"));
        };
        if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(shell_error(
                "chunked loopback readiness responses are forbidden",
            ));
        }
        if name.eq_ignore_ascii_case("content-length") {
            content_length = Some(value.trim().parse::<usize>()?);
        }
    }
    let body = &bytes[split + 4..];
    let expected = content_length
        .ok_or_else(|| shell_error("loopback HTTP response has no Content-Length"))?;
    if expected != body.len() {
        return Err(shell_error(
            "loopback HTTP response body length does not match",
        ));
    }
    Ok(HttpResponse {
        status,
        body: body.to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_origin_accepts_only_explicit_ipv4_loopback_http() {
        assert_eq!(
            exact_loopback_origin("http://127.0.0.1:7777/")
                .unwrap()
                .as_str(),
            "http://127.0.0.1:7777/"
        );
        for rejected in [
            "https://127.0.0.1:7777",
            "http://localhost:7777",
            "http://0.0.0.0:7777",
            "http://127.0.0.1",
            "http://127.0.0.1:7777/path",
            "http://user@127.0.0.1:7777",
            "http://127.0.0.1:7777?query=1",
        ] {
            assert!(exact_loopback_origin(rejected).is_err(), "{rejected}");
        }
    }

    #[test]
    fn navigation_is_confined_to_one_exact_origin() {
        let expected = exact_loopback_origin("http://127.0.0.1:7777").unwrap();
        for allowed in [
            "http://127.0.0.1:7777/",
            "http://127.0.0.1:7777/login",
            "http://127.0.0.1:7777/api/health?x=1#fragment",
        ] {
            assert!(navigation_is_allowed(
                &expected,
                &Url::parse(allowed).unwrap()
            ));
        }
        for rejected in [
            "https://127.0.0.1:7777/",
            "http://localhost:7777/",
            "http://127.0.0.1:7778/",
            "https://example.com/",
            "file:///tmp/host-file",
            "data:text/html,hostile",
            "blob:http://127.0.0.1:7777/id",
        ] {
            assert!(
                !navigation_is_allowed(&expected, &Url::parse(rejected).unwrap()),
                "{rejected}"
            );
        }
    }

    #[test]
    fn readiness_proof_binds_every_security_field() {
        let origin = exact_loopback_origin("http://127.0.0.1:49193").unwrap();
        let secret = [0x11; 32];
        let challenge = hex::encode([0x22; 32]);
        let mut payload = ReadinessPayload {
            schema_version: 1,
            ready: true,
            auth_enabled: true,
            origin: "http://127.0.0.1:49193".into(),
            pid: 123,
            proof: String::new(),
        };
        let mut mac = Hmac::<Sha256>::new_from_slice(&secret).unwrap();
        mac.update(&proof_message(&payload, &challenge));
        payload.proof = hex::encode(mac.finalize().into_bytes());

        verify_readiness_payload(&payload, 200, &origin, 123, &secret, &challenge).unwrap();
        payload.pid = 124;
        assert!(
            verify_readiness_payload(&payload, 200, &origin, 123, &secret, &challenge).is_err()
        );
    }

    #[test]
    fn bounded_http_parser_requires_content_length_and_exact_body() {
        let parsed = parse_http_response(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}",
        )
        .unwrap();
        assert_eq!(parsed.status, 200);
        assert_eq!(parsed.body, b"{}");
        assert!(parse_http_response(b"HTTP/1.1 200 OK\r\n\r\n{}").is_err());
        assert!(parse_http_response(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n{}").is_err());
        assert!(parse_http_response(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n{}"
        )
        .is_err());
    }
}
