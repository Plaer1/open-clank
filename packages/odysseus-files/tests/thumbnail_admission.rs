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
    #[cfg(target_os = "linux")]
    return PathBuf::from(env!("CARGO_BIN_EXE_odysseus-linux-thumbnail-helper"));
    #[cfg(not(target_os = "linux"))]
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

#[cfg(any(target_os = "macos", target_os = "linux"))]
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

#[cfg(any(target_os = "macos", target_os = "linux"))]
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

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
#[test]
fn unsupported_builds_advertise_no_native_thumbnail_helper() {
    assert!(!ThumbnailBroker::supported_on_this_host());
}

#[cfg(target_os = "linux")]
#[test]
fn linux_deadline_and_cancel_kill_descendants_holding_output_pipe() {
    use std::os::unix::fs::PermissionsExt;
    for cancel in [false,true] {
        let directory = tempdir().unwrap();
        let image = directory.path().join("source.png"); fs::write(&image, TEST_PNG).unwrap();
        let pid_file = directory.path().join("descendant.pid");
        let helper = directory.path().join("hanging-helper");
        let program = format!("#!/usr/bin/python3\nimport sys,struct,subprocess,time\np=b'OQL1\\x10'\nsys.stdout.buffer.write(struct.pack('>I',len(p))+p);sys.stdout.buffer.flush()\nn=struct.unpack('>I',sys.stdin.buffer.read(4))[0];sys.stdin.buffer.read(n)\np=subprocess.Popen(['/bin/sleep','30'])\nopen({:?},'w').write(str(p.pid))\ntime.sleep(30)\n",pid_file.to_str().unwrap());
        fs::write(&helper,program).unwrap(); fs::set_permissions(&helper,fs::Permissions::from_mode(0o700)).unwrap();
        let (engine,scope) = scoped_engine(directory.path(),41);
        let broker = Arc::new(ThumbnailBroker::new(ThumbnailBrokerConfig::new(helper)).unwrap());
        let token = ThumbnailCancellationToken::new(); let worker_token=token.clone(); let worker_broker=broker.clone();
        let start=std::time::Instant::now();
        let render=std::thread::spawn(move || worker_broker.render_app(&engine,&scope,&image,&PolicyGeneration::new(41),ThumbnailRequest::new(32,32,1.).with_deadline(Duration::from_millis(500)),worker_token));
        let wait_until=std::time::Instant::now()+Duration::from_secs(2);
        while !pid_file.exists() && std::time::Instant::now()<wait_until {std::thread::sleep(Duration::from_millis(5));}
        assert!(pid_file.exists());
        let pid=fs::read_to_string(&pid_file).unwrap();
        if cancel { token.cancel(); }
        assert_eq!(render.join().unwrap(),Err(if cancel {ThumbnailError::Cancelled} else {ThumbnailError::DeadlineExceeded}));
        assert!(start.elapsed()<Duration::from_secs(2),"inherited output pipe must not hang reader join");
        drop(broker);
        if let Ok(stat)=fs::read_to_string(format!("/proc/{pid}/stat")) {
            assert_eq!(stat.split_whitespace().nth(2),Some("Z"),"descendant must not remain running");
        }
    }
}

#[cfg(target_os = "linux")]
#[test]
#[ignore = "requires the optional KDE adapter and host desktop IPC; use isolated XDG paths"]
fn linux_native_content_admission_bounds_and_revalidation() {
    use std::os::unix::fs::PermissionsExt;
    let directory=tempdir().unwrap();
    let image=directory.path().join("native é %#?[] image.png");
    { let mut encoder=png::Encoder::new(fs::File::create(&image).unwrap(),96,48);
      encoder.set_color(png::ColorType::Rgba); encoder.set_depth(png::BitDepth::Eight);
      encoder.write_header().unwrap().write_image_data(&[200,30,70,255].repeat(96*48)).unwrap(); }
    let (engine,scope)=scoped_engine(directory.path(),51);
    let broker=ThumbnailBroker::new(ThumbnailBrokerConfig::new(helper_path())).unwrap();
    let generation=PolicyGeneration::new(51);
    let render=|broker: &ThumbnailBroker| broker.render_app(&engine,&scope,&image,&generation,ThumbnailRequest::new(32,32,1.).with_deadline(Duration::from_secs(8)),ThumbnailCancellationToken::new());
    let result=render(&broker).unwrap();
    let mut reader=png::Decoder::new(std::io::Cursor::new(result.bytes)).read_info().unwrap();
    let mut bytes=vec![0;reader.output_buffer_size()]; let frame=reader.next_frame(&mut bytes).unwrap();
    assert_eq!((frame.width,frame.height),(32,16)); assert_eq!(&bytes[..4],&[200,30,70,255]);
    assert_eq!(broker.render_app(&engine,&scope,directory.path(),&generation,ThumbnailRequest::new(32,32,1.),ThumbnailCancellationToken::new()),Err(ThumbnailError::NotEligible));
    let outside=tempdir().unwrap(); let denied=outside.path().join("denied.png");fs::write(&denied,TEST_PNG).unwrap();
    assert_eq!(broker.render_app(&engine,&scope,&denied,&generation,ThumbnailRequest::new(32,32,1.),ThumbnailCancellationToken::new()),Err(ThumbnailError::Denied));
    let mut limits=ThumbnailBrokerConfig::new(helper_path());limits.limits.max_output_bytes=32;
    assert_eq!(render(&ThumbnailBroker::new(limits).unwrap()),Err(ThumbnailError::OutputTooLarge));
    // Delay helper startup to reproduce an authorized source being replaced
    // after admission, with real native content returned for the replacement.
    let wrapper=directory.path().join("delayed-helper");
    let escaped=helper_path().to_string_lossy().replace('\'',"'\\''");
    fs::write(&wrapper,format!("#!/bin/sh\nsleep 0.3\nexec '{escaped}'\n")).unwrap();
    fs::set_permissions(&wrapper,fs::Permissions::from_mode(0o700)).unwrap();
    let delayed=ThumbnailBroker::new(ThumbnailBrokerConfig::new(wrapper)).unwrap();
    std::thread::scope(|threads| {
        let worker=threads.spawn(||render(&delayed));
        std::thread::sleep(Duration::from_millis(80));
        let replacement=directory.path().join("replacement.png");fs::copy(&image,&replacement).unwrap();fs::rename(&replacement,&image).unwrap();
        assert_eq!(worker.join().unwrap(),Err(ThumbnailError::Stale));
    });
    assert_eq!(delayed.stats().stale_completions,1);
}
