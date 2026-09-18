#![cfg(unix)]

use odysseus_files::{
    Capability, CaptureAfter, CaptureTarget, EngineConfig, FileEngine, HistoryServiceHook,
    MutationCaptureHook, MutationCaptureTicket, PlatformIdentity, RootAvailability, RootKind,
    RootRecord, RootRegistry,
};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use tempfile::tempdir;

#[derive(Clone, Debug)]
enum CaptureEvent {
    Prepare {
        operation: String,
        targets: Vec<(std::path::PathBuf, Option<Vec<u8>>)>,
    },
    Complete {
        action_id: String,
        after: Vec<(std::path::PathBuf, Option<Vec<u8>>)>,
    },
    Abort {
        action_id: String,
    },
}

#[derive(Clone, Default)]
struct RecordingCaptureHook {
    events: Arc<Mutex<Vec<CaptureEvent>>>,
    next_action: Arc<Mutex<usize>>,
    fail_complete: bool,
}

struct RecordingCaptureTicket {
    action_id: String,
    events: Arc<Mutex<Vec<CaptureEvent>>>,
    fail_complete: bool,
}

impl MutationCaptureTicket for RecordingCaptureTicket {
    fn action_id(&self) -> &str {
        &self.action_id
    }

    fn complete(self: Box<Self>, after: Vec<CaptureAfter>) -> Result<(), String> {
        self.events.lock().unwrap().push(CaptureEvent::Complete {
            action_id: self.action_id,
            after: after
                .into_iter()
                .map(|item| (item.resource_id, item.after))
                .collect(),
        });
        if self.fail_complete {
            Err("injected after capture failure".into())
        } else {
            Ok(())
        }
    }

    fn abort(self: Box<Self>) {
        self.events.lock().unwrap().push(CaptureEvent::Abort {
            action_id: self.action_id,
        });
    }
}

impl MutationCaptureHook for RecordingCaptureHook {
    fn prepare(
        &self,
        operation: &str,
        targets: &[CaptureTarget],
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        let mut next = self.next_action.lock().unwrap();
        *next += 1;
        let action_id = format!("file-action-{}", *next);
        self.events.lock().unwrap().push(CaptureEvent::Prepare {
            operation: operation.into(),
            targets: targets
                .iter()
                .map(|item| (item.resource_id.clone(), item.before.clone()))
                .collect(),
        });
        Ok(Box::new(RecordingCaptureTicket {
            action_id,
            events: Arc::clone(&self.events),
            fail_complete: self.fail_complete,
        }))
    }
}

#[test]
fn provider_engine_sends_authenticated_before_live_and_after_frames() {
    let root = tempdir().unwrap();
    let socket = root.path().join("history.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    let frames = Arc::new(Mutex::new(Vec::<serde_json::Value>::new()));
    let captured = Arc::clone(&frames);
    let server = thread::spawn(move || {
        for _ in 0..4 {
            let (mut stream, _) = listener.accept().unwrap();
            let mut bytes = String::new();
            BufReader::new(&mut stream).read_line(&mut bytes).unwrap();
            let frame: serde_json::Value = serde_json::from_str(bytes.trim()).unwrap();
            let response = if frame.get("RegisterResource").is_some() {
                serde_json::json!({
                    "Resource": {
                        "handle": {
                            "resource_id": "file:registry:test",
                            "account_id": "account-1",
                            "workspace_id": "workspace-1",
                            "generation": 1
                        },
                        "created": true
                    }
                })
            } else {
                serde_json::json!({})
            };
            captured.lock().unwrap().push(frame);
            stream
                .write_all(format!("{}\n", response).as_bytes())
                .unwrap();
        }
    });

    let path = root.path().join("note.txt");
    std::fs::write(&path, b"before").unwrap();
    let snapshot = std::fs::read(&path).unwrap();
    let hook = HistoryServiceHook::new(
        &socket,
        "actor-1",
        "account-1",
        "workspace-1",
        root.path(),
        "supervisor-token",
    )
    .unwrap();
    let engine =
        FileEngine::new(EngineConfig::default(), registry_for(root.path())).with_capture_hook(hook);
    let mut digest = Sha256::new();
    digest.update(&snapshot);
    let expected = odysseus_files::Fingerprint {
        algorithm: "sha256",
        value: format!("sha256:{:x}:{}", digest.finalize(), snapshot.len()),
    };
    engine
        .replace_if_fingerprint_app(&path, Some(&expected), b"after")
        .unwrap();
    server.join().unwrap();

    let frames = frames.lock().unwrap();
    assert_eq!(frames.len(), 4);
    let action_ids = frames
        .iter()
        .skip(1)
        .map(|frame| {
            frame
                .pointer("/Prepare/envelope/request/action_id")
                .or_else(|| frame.pointer("/RecordLive/envelope/action_id"))
                .or_else(|| frame.pointer("/Complete/envelope/action_id"))
                .and_then(serde_json::Value::as_str)
                .unwrap()
                .to_owned()
        })
        .collect::<Vec<_>>();
    assert!(action_ids.windows(2).all(|pair| pair[0] == pair[1]));
    assert_eq!(
        frames[1]["Prepare"]["envelope"]["auth"]["actor_id"],
        "actor-1"
    );
    assert_eq!(
        frames[1]["Prepare"]["envelope"]["auth"]["account_id"],
        "account-1"
    );
    assert_eq!(
        frames[1]["Prepare"]["envelope"]["request"]["resource_key"]["resource_id"],
        "file:registry:test"
    );
}

#[test]
fn provider_engine_capture_matrix_covers_file_lifecycle_and_multitarget_failures() {
    let root = tempdir().unwrap();
    let source = root.path().join("source.txt");
    let copy = root.path().join("copy.txt");
    let moved = root.path().join("moved.txt");
    let hook = RecordingCaptureHook {
        events: Arc::new(Mutex::new(Vec::new())),
        next_action: Arc::new(Mutex::new(0)),
        fail_complete: true,
    };
    let events = Arc::clone(&hook.events);
    let engine = FileEngine::new(EngineConfig::default(), RootRegistry::default())
        .with_capture_hook(Arc::new(hook));

    assert!(matches!(
        engine.create_file_app(&source, b"created").unwrap(),
        odysseus_files::ReplaceOutcome::Created { .. }
    ));
    let expected = engine.stat_app(&source, true).unwrap().fingerprint;
    engine
        .replace_if_fingerprint_app(&source, expected.as_ref(), b"replaced")
        .unwrap();
    engine.copy_file_app(&source, &copy).unwrap();
    engine.move_file_app(&copy, &moved).unwrap();
    let trash = engine.trash_file_app(&moved).unwrap();
    engine.restore_file_app(&trash).unwrap();
    assert_eq!(std::fs::read(&moved).unwrap(), b"replaced");

    let events = events.lock().unwrap().clone();
    let operations = events
        .iter()
        .filter_map(|event| match event {
            CaptureEvent::Prepare { operation, .. } => Some(operation.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        operations,
        vec!["create", "replace", "copy", "move", "trash", "restore"]
    );
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event, CaptureEvent::Complete { .. }))
            .count(),
        operations.len()
    );
    assert!(events.iter().all(|event| match event {
        CaptureEvent::Complete { action_id, after } => {
            !action_id.is_empty() && !after.is_empty()
        }
        CaptureEvent::Prepare { targets, .. } => !targets.is_empty(),
        CaptureEvent::Abort { action_id } => !action_id.is_empty(),
    }));
    assert!(events.iter().any(|event| matches!(
        event,
        CaptureEvent::Prepare { operation, targets }
            if operation == "move" && targets.len() == 2
    )));
    assert!(events.iter().any(|event| matches!(
        event,
        CaptureEvent::Complete { after, .. }
            if after.iter().any(|(_, bytes)| bytes.is_none())
    )));
}

fn registry_for(root: &Path) -> RootRegistry {
    let canonical = std::fs::canonicalize(root).unwrap();
    let mut registry = RootRegistry::default();
    registry
        .register(RootRecord {
            id: "root-1".into(),
            owner_id: "owner-1".into(),
            kind: RootKind::RecursiveDirectory,
            canonical_path: canonical.to_string_lossy().into_owned(),
            display_path: canonical.to_string_lossy().into_owned(),
            enabled: true,
            capabilities: [Capability::Read, Capability::Write].into_iter().collect(),
            platform_identity: PlatformIdentity {
                volume_id: None,
                file_id: None,
                device: None,
                inode: None,
                case_sensitive: Some(true),
            },
            last_validated_unix_ms: None,
            availability: RootAvailability::Available,
        })
        .unwrap();
    registry
}
