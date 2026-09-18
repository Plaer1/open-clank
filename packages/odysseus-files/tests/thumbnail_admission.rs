use odysseus_files::{
    AppScope, Capability, EngineConfig, FileEngine, PlatformIdentity, PolicyGeneration,
    RootAvailability, RootKind, RootRecord, RootRegistry, ThumbnailBroker, ThumbnailBrokerConfig,
    ThumbnailCancellationToken, ThumbnailError, ThumbnailRequest,
};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::Duration;
use tempfile::tempdir;

const TEST_PNG: &[u8] = &[
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4,
    0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0xf0,
    0x1f, 0x00, 0x05, 0x00, 0x01, 0xff, 0x89, 0x99, 0x3d, 0x1d, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
    0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
];

fn helper_path() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_odysseus-quicklook-helper"))
}

fn scoped_engine(root: &Path, generation: u64) -> (FileEngine, AppScope) {
    let canonical = fs::canonicalize(root).unwrap();
    let mut registry = RootRegistry::default();
    registry
        .register(RootRecord {
            id: "thumbnail-root".into(),
            owner_id: "owner-1".into(),
            kind: RootKind::RecursiveDirectory,
            canonical_path: canonical.to_string_lossy().into_owned(),
            display_path: "approved files".into(),
            enabled: true,
            capabilities: BTreeSet::from([Capability::Read]),
            platform_identity: PlatformIdentity {
                volume_id: None,
                file_id: None,
                device: None,
                inode: None,
                case_sensitive: Some(true),
            },
            last_validated_unix_ms: Some(1),
            availability: RootAvailability::Available,
        })
        .unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry);
    let scope = AppScope::assigned(
        ["thumbnail-root".to_string()],
        [Capability::Read],
        generation,
    );
    (engine, scope)
}

#[cfg(target_os = "macos")]
#[test]
fn helper_refuses_direct_invocation_without_its_private_channel() {
    let output = Command::new(helper_path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn denial_directories_and_package_directories_never_start_quicklook() {
    let approved = tempdir().unwrap();
    let outside = tempdir().unwrap();
    let package = approved.path().join("Example.app");
    fs::create_dir(&package).unwrap();
    let approved_image = approved.path().join("approved.png");
    fs::write(&approved_image, TEST_PNG).unwrap();
    let secret = outside.path().join("secret.png");
    fs::write(&secret, TEST_PNG).unwrap();
    let (engine, scope) = scoped_engine(approved.path(), 7);
    let broker = ThumbnailBroker::new(ThumbnailBrokerConfig::new(helper_path())).unwrap();
    let generation = PolicyGeneration::new(7);
    let request = ThumbnailRequest::new(64, 64, 1.0);

    assert_eq!(
        broker.render_app(
            &engine,
            &scope,
            &secret,
            &generation,
            request.clone(),
            ThumbnailCancellationToken::new(),
        ),
        Err(ThumbnailError::Denied)
    );
    assert_eq!(
        broker.render_app(
            &engine,
            &scope,
            &package,
            &generation,
            request,
            ThumbnailCancellationToken::new(),
        ),
        Err(ThumbnailError::NotEligible)
    );
    let cancellation = ThumbnailCancellationToken::new();
    cancellation.cancel();
    assert_eq!(
        broker.render_app(
            &engine,
            &scope,
            &approved_image,
            &generation,
            ThumbnailRequest::new(64, 64, 1.0),
            cancellation,
        ),
        Err(ThumbnailError::Cancelled)
    );
    assert_eq!(broker.stats().helper_starts, 0);
}

#[cfg(target_os = "macos")]
#[test]
fn objc2_helper_returns_bounded_content_png_and_enforces_deadline() {
    let directory = tempdir().unwrap();
    let image = directory.path().join("pixel.png");
    fs::write(&image, TEST_PNG).unwrap();
    let (engine, scope) = scoped_engine(directory.path(), 11);
    let broker = ThumbnailBroker::new(ThumbnailBrokerConfig::new(helper_path())).unwrap();
    let generation = PolicyGeneration::new(11);

    let png = broker
        .render_app(
            &engine,
            &scope,
            &image,
            &generation,
            ThumbnailRequest::new(128, 128, 2.0).with_deadline(Duration::from_secs(10)),
            ThumbnailCancellationToken::new(),
        )
        .unwrap();
    assert!(png.bytes.starts_with(b"\x89PNG\r\n\x1a\n"));
    assert!(png.bytes.len() <= 4 * 1024 * 1024);
    assert_eq!(png.scale_milli, 2000);

    assert_eq!(
        broker.render_app(
            &engine,
            &scope,
            &image,
            &generation,
            ThumbnailRequest::new(128, 128, 1.0).with_deadline(Duration::ZERO),
            ThumbnailCancellationToken::new(),
        ),
        Err(ThumbnailError::DeadlineExceeded)
    );

    let mut capped_config = ThumbnailBrokerConfig::new(helper_path());
    capped_config.limits.max_output_bytes = 32;
    let capped = ThumbnailBroker::new(capped_config).unwrap();
    assert_eq!(
        capped.render_app(
            &engine,
            &scope,
            &image,
            &generation,
            ThumbnailRequest::new(128, 128, 1.0),
            ThumbnailCancellationToken::new(),
        ),
        Err(ThumbnailError::OutputTooLarge)
    );
    assert_eq!(capped.stats().output_rejections, 1);
}

#[cfg(target_os = "macos")]
#[test]
fn replacement_during_private_handoff_is_discarded_after_identity_revalidation() {
    use std::os::unix::fs::PermissionsExt;

    let directory = tempdir().unwrap();
    let image = directory.path().join("replace-me.png");
    fs::write(&image, TEST_PNG).unwrap();
    let wrapper = directory.path().join("delayed-helper");
    let escaped_helper = helper_path().to_string_lossy().replace('\'', "'\\''");
    fs::write(
        &wrapper,
        format!("#!/bin/sh\nsleep 0.25\nexec '{escaped_helper}'\n"),
    )
    .unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o700)).unwrap();

    let (engine, scope) = scoped_engine(directory.path(), 19);
    let engine = Arc::new(engine);
    let broker = Arc::new(ThumbnailBroker::new(ThumbnailBrokerConfig::new(wrapper)).unwrap());
    let generation = PolicyGeneration::new(19);
    let worker_engine = Arc::clone(&engine);
    let worker_broker = Arc::clone(&broker);
    let worker_scope = scope.clone();
    let worker_generation = generation.clone();
    let worker_image = image.clone();
    let render = std::thread::spawn(move || {
        worker_broker.render_app(
            &worker_engine,
            &worker_scope,
            &worker_image,
            &worker_generation,
            ThumbnailRequest::new(96, 96, 1.0).with_deadline(Duration::from_secs(10)),
            ThumbnailCancellationToken::new(),
        )
    });

    std::thread::sleep(Duration::from_millis(60));
    fs::rename(&image, directory.path().join("old.png")).unwrap();
    fs::write(&image, TEST_PNG).unwrap();

    assert_eq!(render.join().unwrap(), Err(ThumbnailError::Stale));
    assert_eq!(broker.stats().stale_completions, 1);
}

#[cfg(target_os = "macos")]
#[test]
fn supervisor_queue_rejects_excess_work_without_unbounded_buffering() {
    use std::os::unix::fs::PermissionsExt;

    let directory = tempdir().unwrap();
    let image = directory.path().join("queued.png");
    fs::write(&image, TEST_PNG).unwrap();
    let wrapper = directory.path().join("slow-start-helper");
    let escaped_helper = helper_path().to_string_lossy().replace('\'', "'\\''");
    fs::write(
        &wrapper,
        format!("#!/bin/sh\nsleep 0.4\nexec '{escaped_helper}'\n"),
    )
    .unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o700)).unwrap();

    let (engine, scope) = scoped_engine(directory.path(), 29);
    let engine = Arc::new(engine);
    let mut config = ThumbnailBrokerConfig::new(wrapper);
    config.limits.queue_capacity = 1;
    let broker = Arc::new(ThumbnailBroker::new(config).unwrap());
    let generation = PolicyGeneration::new(29);

    let spawn_render = |engine: Arc<FileEngine>, broker: Arc<ThumbnailBroker>| {
        let scope = scope.clone();
        let generation = generation.clone();
        let image = image.clone();
        std::thread::spawn(move || {
            broker.render_app(
                &engine,
                &scope,
                &image,
                &generation,
                ThumbnailRequest::new(64, 64, 1.0).with_deadline(Duration::from_secs(10)),
                ThumbnailCancellationToken::new(),
            )
        })
    };

    let first = spawn_render(Arc::clone(&engine), Arc::clone(&broker));
    std::thread::sleep(Duration::from_millis(50));
    let second = spawn_render(Arc::clone(&engine), Arc::clone(&broker));
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(
        broker.render_app(
            &engine,
            &scope,
            &image,
            &generation,
            ThumbnailRequest::new(64, 64, 1.0),
            ThumbnailCancellationToken::new(),
        ),
        Err(ThumbnailError::QueueFull)
    );
    assert!(first.join().unwrap().is_ok());
    assert!(second.join().unwrap().is_ok());
    assert_eq!(broker.stats().queue_rejections, 1);
}

#[cfg(target_os = "macos")]
#[test]
fn crashed_helper_is_restarted_only_inside_the_bounded_budget() {
    let directory = tempdir().unwrap();
    let image = directory.path().join("pixel.png");
    fs::write(&image, TEST_PNG).unwrap();
    let (engine, scope) = scoped_engine(directory.path(), 23);
    let mut config = ThumbnailBrokerConfig::new("/usr/bin/false");
    config.limits.max_restarts_per_job = 1;
    let broker = ThumbnailBroker::new(config).unwrap();

    assert_eq!(
        broker.render_app(
            &engine,
            &scope,
            &image,
            &PolicyGeneration::new(23),
            ThumbnailRequest::new(64, 64, 1.0),
            ThumbnailCancellationToken::new(),
        ),
        Err(ThumbnailError::HelperUnavailable)
    );
    let stats = broker.stats();
    assert_eq!(stats.helper_starts, 2);
    assert_eq!(stats.helper_restarts, 1);
}

#[cfg(not(target_os = "macos"))]
#[test]
fn non_macos_builds_advertise_no_native_thumbnail_helper() {
    assert!(!ThumbnailBroker::supported_on_this_host());
}
