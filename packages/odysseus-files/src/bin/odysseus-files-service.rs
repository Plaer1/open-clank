#[cfg(unix)]
use odysseus_files::LocalIpcServer;
use odysseus_files::{
    AgentScope, AppScope, ClientRequest, DEFAULT_MAX_FRAME_BYTES, EngineConfig, FileEngine,
    FileService, FrameDecoder, HistoryServiceHook, RootRegistry, ServiceIdentity, ThumbnailBroker,
    ThumbnailBrokerConfig, TrustLane, encode_frame,
};
use serde_json::Value;
use std::env;
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::sync::Arc;

fn main() -> io::Result<()> {
    let owner_id = env::var("ODYSSEUS_FILES_OWNER_ID").unwrap_or_else(|_| "default-owner".into());
    let principal_id =
        env::var("ODYSSEUS_FILES_PRINCIPAL_ID").unwrap_or_else(|_| "odysseus-app".into());
    let session_id =
        env::var("ODYSSEUS_FILES_SESSION_ID").unwrap_or_else(|_| "service-session".into());
    let lane = match env::var("ODYSSEUS_FILES_LANE")
        .unwrap_or_else(|_| "app".into())
        .as_str()
    {
        "app" => TrustLane::App,
        "agent" => TrustLane::Agent,
        other => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("unknown ODYSSEUS_FILES_LANE: {other}"),
            ));
        }
    };
    let registry = load_registry()?;
    let scope = load_scope()?;
    let app_scope = match lane {
        TrustLane::App => load_app_scope()?,
        TrustLane::Agent => AppScope::host(),
    };
    let identity = match lane {
        TrustLane::App => {
            ServiceIdentity::app_with_scope(owner_id, principal_id, session_id, app_scope)
        }
        TrustLane::Agent => ServiceIdentity::agent(
            owner_id,
            principal_id,
            session_id,
            scope,
            [
                odysseus_files::Capability::Read,
                odysseus_files::Capability::Write,
            ],
        ),
    };
    // The supervisor may inject an authenticated history endpoint and a
    // server-minted workspace binding. The Files service derives actor and
    // account from its already-authenticated ServiceIdentity; callers cannot
    // provide those fields through mutation requests. A configured endpoint
    // with a missing or invalid binding blocks mutations; reads remain available.
    // Explicitly unconfigured capture keeps its optional behavior.
    let history_required = env::var("OPENCLANK_HISTORY_SOCKET").ok().is_some_and(|socket| !socket.trim().is_empty());
    let history_hook = env::var_os("OPENCLANK_HISTORY_SOCKET").and_then(|socket| {
        let workspace_root = env::var_os("OPENCLANK_HISTORY_WORKSPACE_ROOT")?;
        let workspace_id = env::var("OPENCLANK_HISTORY_WORKSPACE_ID").ok()?;
        let token = env::var("OPENCLANK_HISTORY_TOKEN")
            .ok()
            .filter(|value| !value.trim().is_empty())?;
        let actor_kind = match lane {
            TrustLane::App => "human",
            TrustLane::Agent => "agent",
        };
        let history_actor = env::var("OPENCLANK_HISTORY_ACTOR_ID")
            .unwrap_or_else(|_| identity.principal_id().to_owned());
        let history_account = env::var("OPENCLANK_HISTORY_ACCOUNT_ID")
            .unwrap_or_else(|_| identity.owner_id().to_owned());
        match HistoryServiceHook::new_with_actor_kind(
            socket,
            history_actor,
            history_account,
            workspace_id,
            workspace_root,
            token,
            actor_kind,
        ) {
            Ok(hook) => Some(hook),
            Err(error) => {
                eprintln!("odysseus history capture paused: {error}");
                None
            }
        }
    });
    let mut service =
        FileService::new(FileEngine::new(EngineConfig::default(), registry).require_capture(history_required), identity);
    if let Some(history_hook) = history_hook {
        service = service.with_history_capture(history_hook);
    }
    #[cfg(target_os = "macos")]
    if let Some(helper_path) = quicklook_helper_path()? {
        let broker = ThumbnailBroker::new(ThumbnailBrokerConfig::new(helper_path))
            .map_err(|error| io::Error::other(error.to_string()))?;
        service = service.with_thumbnail_broker(broker);
    }
    #[cfg(windows)]
    if let Some(helper_path) = shell_thumbnail_helper_path()? {
        let mut icons = ThumbnailBrokerConfig::new(&helper_path);
        icons.shell_icons = true;
        let icon_broker = ThumbnailBroker::new(icons).map_err(|error| io::Error::other(error.to_string()))?;
        let broker = ThumbnailBroker::new(ThumbnailBrokerConfig::new(helper_path))
            .map_err(|error| io::Error::other(error.to_string()))?;
        service = service.with_thumbnail_broker(broker).with_native_icon_broker(icon_broker);
    }
    let service = Arc::new(service);

    if let Some(socket_path) = env::var_os("ODYSSEUS_FILES_GRPC_SOCKET") {
        #[cfg(all(feature = "tonic-transport", any(target_os = "macos", windows)))]
        {
            return run_grpc(service, socket_path.into());
        }
        #[cfg(not(all(feature = "tonic-transport", any(target_os = "macos", windows))))]
        {
            let _ = (service, socket_path);
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Tonic filesystem transport requires a feature-enabled macOS or Windows build",
            ));
        }
    }
    if let Ok(socket_path) = env::var("ODYSSEUS_FILES_SOCKET") {
        #[cfg(unix)]
        {
            return run_socket(service, socket_path);
        }
        #[cfg(not(unix))]
        {
            let _ = socket_path;
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "Unix IPC is unavailable on this platform; use stdio",
            ));
        }
    }
    run_stdio(service)
}

#[cfg(target_os = "macos")]
fn quicklook_helper_path() -> io::Result<Option<PathBuf>> {
    if let Some(configured) = env::var_os("ODYSSEUS_QUICKLOOK_HELPER_BIN") {
        let path = PathBuf::from(configured);
        if !path.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "configured Quick Look helper is unavailable",
            ));
        }
        return Ok(Some(path));
    }
    let sibling = env::current_exe()?
        .parent()
        .map(|parent| parent.join("odysseus-quicklook-helper"));
    Ok(sibling.filter(|path| path.is_file()))
}

#[cfg(not(target_os = "macos"))]
#[allow(dead_code)]
fn quicklook_helper_path() -> io::Result<Option<PathBuf>> {
    Ok(None)
}

#[cfg(all(feature = "tonic-transport", any(target_os = "macos", windows)))]
fn run_grpc(service: Arc<FileService>, socket_path: std::path::PathBuf) -> io::Result<()> {
    let session = odysseus_files::grpc_transport::session_binding_from_env()?;
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime
        .block_on(odysseus_files::grpc_transport::serve(
            service,
            socket_path,
            session,
        ))
        .map_err(|error| io::Error::other(error.to_string()))
}

fn load_registry() -> io::Result<RootRegistry> {
    let Some(path) = env::var_os("ODYSSEUS_FILES_REGISTRY") else {
        return Ok(RootRegistry::default());
    };
    let file = File::open(path)?;
    serde_json::from_reader(file).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn load_scope() -> io::Result<AgentScope> {
    let Some(raw) = env::var_os("ODYSSEUS_FILES_SCOPE") else {
        return Ok(AgentScope::default());
    };
    serde_json::from_str(&raw.to_string_lossy())
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn load_app_scope() -> io::Result<AppScope> {
    let raw = env::var_os("ODYSSEUS_FILES_APP_SCOPE").ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "ODYSSEUS_FILES_APP_SCOPE is required for app lane",
        )
    })?;
    serde_json::from_str(&raw.to_string_lossy())
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn dispatch(service: &FileService, bytes: &[u8]) -> Vec<u8> {
    let response: Value = match serde_json::from_slice::<ClientRequest>(bytes) {
        Ok(request) => match service.dispatch(request) {
            Ok(value) => value,
            Err(error) => serde_json::to_value(error).unwrap_or_else(
                |_| serde_json::json!({"code": "unavailable", "message": "service error"}),
            ),
        },
        Err(error) => serde_json::json!({
            "code": "malformed_request",
            "message": "request envelope is invalid",
            "details": {"parse": error.to_string()},
        }),
    };
    serde_json::to_vec(&response).unwrap_or_else(|_| {
        b"{\"code\":\"unavailable\",\"message\":\"serialization failure\"}".to_vec()
    })
}

fn run_stdio(service: Arc<FileService>) -> io::Result<()> {
    let mut decoder = FrameDecoder::new(DEFAULT_MAX_FRAME_BYTES);
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    let mut buffer = [0_u8; 16 * 1024];
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            return Ok(());
        }
        for frame in decoder
            .feed(&buffer[..read])
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?
        {
            let response = encode_frame(&dispatch(&service, &frame), DEFAULT_MAX_FRAME_BYTES)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
            output.write_all(&response)?;
            output.flush()?;
        }
    }
}

#[cfg(unix)]
fn run_socket(service: Arc<FileService>, socket_path: String) -> io::Result<()> {
    let server = LocalIpcServer::bind(socket_path, DEFAULT_MAX_FRAME_BYTES)?;
    eprintln!("odysseus-files-service ready: {}", server.path().display());
    loop {
        let mut connection = server.accept()?;
        let service = Arc::clone(&service);
        std::thread::spawn(move || {
            loop {
                let frame = match connection.read_frame() {
                    Ok(frame) => frame,
                    Err(_) => return,
                };
                let response = dispatch(&service, &frame);
                if connection
                    .write_frame(&response, DEFAULT_MAX_FRAME_BYTES)
                    .is_err()
                {
                    return;
                }
            }
        });
    }
}

#[cfg(windows)]
fn shell_thumbnail_helper_path() -> io::Result<Option<PathBuf>> {
    if let Some(configured) = env::var_os("ODYSSEUS_SHELL_THUMBNAIL_HELPER_BIN") {
        let path = PathBuf::from(configured);
        if !path.is_file() { return Err(io::Error::new(io::ErrorKind::NotFound, "configured Shell thumbnail helper unavailable")); }
        return Ok(Some(path));
    }
    Ok(env::current_exe()?.parent().map(|parent| parent.join("odysseus-shell-thumbnail-helper.exe")).filter(|path| path.is_file()))
}
