#[cfg(feature = "tonic-transport")]
use openclank_agent_supervisor::server::{DriverLaunchSpec, HealthOnlyService};
use openclank_agent_supervisor::{build_id, health_payload};
#[cfg(feature = "tonic-transport")]
use openclank_agent_supervisor::{schema_sha256, validate_runtime_root, RuntimeRootGuard};
use std::env;
#[cfg(feature = "tonic-transport")]
use std::path::PathBuf;

fn usage() {
    eprintln!("usage: openclank-agent-supervisor (--health|--version|serve [--runtime-root PATH] [--driver-program PATH --driver-cwd PATH --driver-arg ARG ...])");
}

#[cfg(feature = "tonic-transport")]
#[tokio::main]
async fn main() {
    let mut args = env::args().skip(1);
    match args.next().as_deref() {
        Some("--version") => println!("openclank-agent-supervisor {}", build_id()),
        Some("--health") => println!("{}", health_payload()),
        Some("serve") => {
            let mut runtime_root = None;
            let mut session_binding = None;
            let mut driver_program = None;
            let mut driver_cwd = None;
            let mut driver_args = Vec::new();
            while let Some(arg) = args.next() {
                if arg == "--runtime-root" {
                    runtime_root = args.next().map(PathBuf::from);
                } else if arg == "--session-binding" {
                    session_binding = args.next();
                } else if arg == "--driver-program" {
                    driver_program = args.next();
                } else if arg == "--driver-cwd" {
                    driver_cwd = args.next().map(PathBuf::from);
                } else if arg == "--driver-arg" {
                    let Some(value) = args.next() else {
                        usage();
                        std::process::exit(2);
                    };
                    driver_args.push(value);
                } else {
                    usage();
                    std::process::exit(2);
                }
            }
            let root = runtime_root.unwrap_or_else(|| {
                env::temp_dir()
                    .join("openclank-agent-supervisor")
                    .join("runtime")
            });
            if let Err(error) = validate_runtime_root(&root) {
                eprintln!("supervisor configuration error: {error}");
                std::process::exit(2);
            }
            let instance_id = format!("{}-{}", std::process::id(), build_id());
            let _guard =
                match RuntimeRootGuard::acquire(&root, instance_id, "unix_uds", schema_sha256()) {
                    Ok(guard) => guard,
                    Err(error) => {
                        eprintln!("supervisor runtime-root error: {error}");
                        std::process::exit(1);
                    }
                };
            let socket = root.join("supervisor.sock");
            let Some(session_binding) = session_binding else {
                eprintln!("supervisor configuration error: --session-binding is required");
                std::process::exit(2);
            };
            let service = match driver_program {
                Some(program) => HealthOnlyService::with_driver_launch(DriverLaunchSpec {
                    program,
                    args: driver_args,
                    cwd: driver_cwd.unwrap_or_else(|| {
                        env::current_dir().expect("current directory must be readable")
                    }),
                }),
                None => HealthOnlyService::default(),
            };
            let serve = openclank_agent_supervisor::server::serve_unix_with_service(
                &socket,
                &session_binding,
                service,
            );
            let heartbeat = async {
                let mut interval = tokio::time::interval(std::time::Duration::from_secs(15));
                interval.tick().await;
                loop {
                    interval.tick().await;
                    _guard.heartbeat()?;
                }
                #[allow(unreachable_code)]
                Ok::<(), openclank_agent_supervisor::SupervisorError>(())
            };
            tokio::pin!(serve);
            tokio::pin!(heartbeat);
            tokio::select! {
                result = &mut serve => {
                    if let Err(error) = result {
                        eprintln!("supervisor serve error: {error}");
                        std::process::exit(1);
                    }
                }
                result = &mut heartbeat => {
                    eprintln!("supervisor manifest heartbeat error: {result:?}");
                    std::process::exit(1);
                }
            }
        }
        _ => {
            usage();
            std::process::exit(2);
        }
    }
}

/// The no-default-features build is the transport-neutral/framed validation
/// artifact. It intentionally cannot start the Tonic daemon, but keeping the
/// binary buildable lets CI exercise the protocol/process surface without
/// pulling the optional server runtime into that target.
#[cfg(not(feature = "tonic-transport"))]
fn main() {
    match env::args().nth(1).as_deref() {
        Some("--version") => println!("openclank-agent-supervisor {}", build_id()),
        Some("--health") => println!("{}", health_payload()),
        _ => {
            usage();
            std::process::exit(2);
        }
    }
}
