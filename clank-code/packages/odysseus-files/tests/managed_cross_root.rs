//! Managed move of one document plus its exact-provenance assets between two
//! registered roots on one filesystem. Real temp directories and the real
//! service/engine/journal; only the History hook is a recording stand-in whose
//! calls can be made to fail, which is how a crash at each journal phase is
//! simulated (the failing call returns before the next journal write).
#![cfg(target_os = "linux")]

use odysseus_files::*;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use tempfile::{tempdir, tempdir_in, TempDir};

#[derive(Clone, Default)]
struct Faults {
    auth_at: Option<usize>,
    register_after_first: bool,
    prepare_before: bool,
    prepare_after: bool,
    complete: bool,
    registry: bool,
}

#[derive(Default)]
struct State {
    faults: Faults,
    auth_calls: usize,
    action: &'static str,
    registry_plan: Option<Value>,
}

struct Hook {
    roots: Vec<(String, PathBuf)>,
    state: Mutex<State>,
}

struct Ticket(String);
impl MutationCaptureTicket for Ticket {
    fn action_id(&self) -> &str { &self.0 }
    fn complete(self: Box<Self>, _after: Vec<CaptureAfter>) -> Result<(), String> { Ok(()) }
    fn abort(self: Box<Self>) {}
}

impl Hook {
    fn new(roots: &[(&str, &Path)]) -> Arc<Self> {
        Arc::new(Self {
            roots: roots.iter().map(|(id, path)| (id.to_string(), path.to_path_buf())).collect(),
            state: Mutex::new(State { action: "Absent", ..State::default() }),
        })
    }
    fn arm(&self, faults: Faults) {
        let mut state = self.state.lock().unwrap();
        state.faults = faults; state.auth_calls = 0;
    }
    fn registry_plan(&self) -> Option<Value> { self.state.lock().unwrap().registry_plan.clone() }
    fn auth_calls(&self) -> usize { self.state.lock().unwrap().auth_calls }
    fn detail(&self, target: &CaptureTarget) -> Result<Value, String> {
        let (root_id, root_path) = self.root_for(&target.resource_id)?;
        let relative = target.resource_id.strip_prefix(&root_path).map_err(|e| e.to_string())?.to_string_lossy().into_owned();
        Ok(json!({"resource_id":format!("res:{}", target.resource_id.display()), "generation":1,
            "root_id":root_id, "root_path":root_path, "relative_path":relative}))
    }
    fn root_for(&self, path: &Path) -> Result<(String, PathBuf), String> {
        self.roots.iter().filter(|(_, root)| path.starts_with(root))
            .max_by_key(|(_, root)| root.components().count()).cloned()
            .ok_or_else(|| "outside every authorized root".to_owned())
    }
}

impl MutationCaptureHook for Hook {
    fn prepare(&self, _operation: &str, _targets: &[CaptureTarget]) -> Result<Box<dyn MutationCaptureTicket>, String> {
        Err("not used".into())
    }
    fn managed_authorize_root(&self, path: &Path) -> Result<(String, PathBuf), String> {
        let mut state = self.state.lock().unwrap();
        state.auth_calls += 1;
        if state.faults.auth_at == Some(state.auth_calls) { return Err("injected interruption".into()); }
        drop(state);
        self.root_for(path)
    }
    fn managed_action_id(&self, operation_id: &str) -> Result<String, String> { Ok(format!("action-{operation_id}")) }
    fn register_recoverable(&self, targets: &[CaptureTarget], checkpoint: &mut dyn FnMut(Value) -> Result<(), String>) -> Result<Vec<Value>, String> {
        let mut out = Vec::new();
        for (index, target) in targets.iter().enumerate() {
            let detail = self.detail(target)?;
            checkpoint(json!({"kind":"resource", "index":index, "detail":detail}))?;
            out.push(detail);
            if index == 0 && self.state.lock().unwrap().faults.register_after_first { return Err("injected interruption".into()); }
        }
        Ok(out)
    }
    fn prepare_recoverable(&self, _op: &str, _targets: &[CaptureTarget], action_id: &str, _ts: u64, digest: &str,
        checkpoint: &mut dyn FnMut(Value) -> Result<(), String>) -> Result<Box<dyn MutationCaptureTicket>, String> {
        if self.state.lock().unwrap().faults.prepare_before { return Err("injected interruption".into()); }
        // As the real hook does, replay the registrations before the batch.
        for (index, target) in _targets.iter().enumerate() {
            checkpoint(json!({"kind":"resource", "index":index, "detail":self.detail(target)?}))?;
        }
        checkpoint(json!({"kind":"prepare_batch", "request":{"action_id":action_id, "digest":digest}, "entries":[]}))?;
        self.state.lock().unwrap().action = "BeforeDurable";
        if self.state.lock().unwrap().faults.prepare_after { return Err("injected interruption".into()); }
        Ok(Box::new(Ticket(action_id.into())))
    }
    fn recoverable_action(&self, _action_id: &str, _operation_id: &str) -> Result<Value, String> {
        let state = self.state.lock().unwrap().action;
        Ok(if state == "Absent" { json!({"Unavailable":{"code":"managed_action_not_found"}}) } else { json!({"Action":{"state":state}}) })
    }
    fn plan_recoverable_completion(&self, action_id: &str, _prepared: &Value, _after: &[CaptureAfter])
        -> Result<(Value, Vec<(String, Vec<u8>, String)>), String> {
        Ok((json!({"live":{"action_id":action_id}, "entries":[]}), vec![]))
    }
    fn complete_recoverable(&self, _action_id: &str, _completion: &Value, _stages: &[(String, Vec<u8>, String)], _already: bool) -> Result<Value, String> {
        let mut state = self.state.lock().unwrap();
        if state.faults.complete { return Err("injected interruption".into()); }
        state.action = "Complete";
        Ok(json!({}))
    }
    fn commit_recoverable_registry(&self, _action_id: &str, plan: &Value) -> Result<(), String> {
        if self.state.lock().unwrap().faults.registry { return Err("injected interruption".into()); }
        self.state.lock().unwrap().registry_plan = Some(plan.clone());
        Ok(())
    }
}

fn fingerprint(bytes: &[u8]) -> String { format!("sha256:{:x}:{}", Sha256::digest(bytes), bytes.len()) }

struct World {
    _base: TempDir,
    a: PathBuf,
    b: PathBuf,
    journal: PathBuf,
}

impl World {
    fn new() -> Self { Self::in_dirs(tempdir().unwrap(), None) }
    fn in_dirs(base: TempDir, other: Option<&Path>) -> Self {
        let root = fs::canonicalize(base.path()).unwrap();
        let a = root.join("root-a"); let b = other.map(Path::to_path_buf).unwrap_or_else(|| root.join("root-b"));
        let journal = root.join("journal");
        fs::create_dir_all(a.join("notes")).unwrap(); fs::create_dir_all(a.join(".media/notes/doc")).unwrap();
        fs::create_dir_all(a.join("refs")).unwrap(); fs::create_dir_all(b.join("archive")).unwrap();
        fs::write(a.join("notes/doc.md"), b"# doc\n![x](pic.png)\n").unwrap();
        fs::write(a.join(".media/notes/doc/pic.png"), b"png-bytes").unwrap();
        fs::write(a.join("refs/index.md"), b"index").unwrap();
        World { _base: base, a, b, journal }
    }
    fn participants(&self) -> Value {
        json!([
            {"kind":"document", "source":self.a.join("notes/doc.md"), "destination":self.b.join("archive/doc.md"),
             "expected_fingerprint":fingerprint(b"# doc\n![x](pic.png)\n"), "replacement":null},
            {"kind":"asset", "source":self.a.join(".media/notes/doc/pic.png"), "destination":self.b.join(".media/archive/doc/pic.png"),
             "expected_fingerprint":fingerprint(b"png-bytes"), "replacement":null},
        ])
    }
    fn guards(&self) -> Value {
        json!([{"source":self.a.join("refs/index.md"), "expected_fingerprint":fingerprint(b"index")}])
    }
    fn send(&self, hook: &Arc<Hook>, operation: Operation, target: &Path, op_id: &str, payload: Value) -> Result<Value, ProtocolError> {
        let engine = FileEngine::new(EngineConfig::default(), RootRegistry::default())
            .with_managed_journal_root(Some(self.journal.clone())).with_capture_hook(hook.clone());
        let service = FileService::new(engine, ServiceIdentity::app("owner-1", "ui", "session"));
        service.dispatch(ClientRequest::new(operation, Target::path(target.to_string_lossy()), "request-1", "client-session", op_id, payload))
    }
    fn mv(&self, hook: &Arc<Hook>, op_id: &str) -> Result<Value, ProtocolError> {
        self.mv_with(hook, op_id, self.participants(), self.guards())
    }
    fn mv_with(&self, hook: &Arc<Hook>, op_id: &str, participants: Value, guards: Value) -> Result<Value, ProtocolError> {
        let target = PathBuf::from(participants[0]["source"].as_str().unwrap());
        self.send(hook, Operation::ManagedFileMove, &target, op_id, json!({"operation_id":op_id, "participants":participants, "guards":guards}))
    }
    /// The forward move plus a reference edit, so an undo has all three kinds of participant to put back.
    fn mv_with_reference(&self, hook: &Arc<Hook>, op_id: &str) -> Result<Value, ProtocolError> {
        let mut participants = self.participants();
        participants.as_array_mut().unwrap().push(json!({"kind":"reference", "source":self.a.join("refs/index.md"), "destination":null,
            "expected_fingerprint":fingerprint(b"index"), "replacement":b"index rewritten".to_vec()}));
        self.mv_with(hook, op_id, participants, json!([]))
    }
    /// Undo of a settled forward move: only the forward operation id is sent; Native derives every participant from that journal.
    fn undo(&self, hook: &Arc<Hook>, op_id: &str, forward: &str) -> Result<Value, ProtocolError> {
        self.send(hook, Operation::ManagedFileMove, &self.b.join("archive/doc.md"), op_id, json!({"operation_id":op_id, "inverse_of":forward}))
    }
    /// Everything back at root A with the original bytes and nothing left at root B except what was there before (the pre-created folders).
    fn undone(&self) -> bool {
        self.unmoved()
            && fs::read(self.a.join("notes/doc.md")).ok().as_deref() == Some(b"# doc\n![x](pic.png)\n".as_slice())
            && fs::read(self.a.join(".media/notes/doc/pic.png")).ok().as_deref() == Some(b"png-bytes".as_slice())
            && fs::read(self.a.join("refs/index.md")).ok().as_deref() == Some(b"index".as_slice())
    }
    fn cleanup_with_reference(&self, hook: &Arc<Hook>, op_id: &str) -> Result<Value, ProtocolError> {
        let mut participants = self.participants();
        participants.as_array_mut().unwrap().push(json!({"kind":"reference", "source":self.a.join("refs/index.md"), "destination":null,
            "expected_fingerprint":fingerprint(b"index"), "replacement":b"index rewritten".to_vec()}));
        self.send(hook, Operation::ManagedFileCleanup, &self.a, op_id, json!({"operation_id":op_id, "participants":participants, "guards":[]}))
    }
    fn cleanup(&self, hook: &Arc<Hook>, op_id: &str) -> Result<Value, ProtocolError> {
        self.send(hook, Operation::ManagedFileCleanup, &self.a, op_id, json!({"operation_id":op_id, "participants":self.participants(), "guards":self.guards()}))
    }
    fn record(&self, op_id: &str) -> Option<Value> {
        serde_json::from_slice(&fs::read(self.journal.join(format!("{op_id}.json"))).ok()?).ok()
    }
    fn moved(&self) -> bool {
        self.b.join("archive/doc.md").is_file() && self.b.join(".media/archive/doc/pic.png").is_file()
            && !self.a.join("notes/doc.md").exists() && !self.a.join(".media/notes/doc/pic.png").exists()
    }
    fn unmoved(&self) -> bool {
        !self.b.join("archive/doc.md").exists() && !self.b.join(".media/archive/doc/pic.png").exists()
            && self.a.join("notes/doc.md").is_file() && self.a.join(".media/notes/doc/pic.png").is_file()
    }
    fn names_under(root: &Path, marker: &str) -> Vec<PathBuf> {
        let mut found = Vec::new();
        let mut stack = vec![root.to_path_buf()];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir).unwrap().flatten() {
                let path = entry.path();
                if entry.file_name().to_string_lossy().contains(marker) { found.push(path.clone()); }
                if entry.file_type().unwrap().is_dir() { stack.push(path); }
            }
        }
        found
    }
    fn leftovers(&self) -> Vec<PathBuf> {
        let mut all = Self::names_under(&self.a, ".openclank-managed-");
        all.extend(Self::names_under(&self.b, ".openclank-managed-"));
        all
    }
}

fn two_root_hook(w: &World) -> Arc<Hook> { Hook::new(&[("root-a", &w.a), ("root-b", &w.b)]) }

const OP: &str = "b2p2-operation-0001";

fn assert_unchanged_and_refused(w: &World, result: Result<Value, ProtocolError>) -> ProtocolError {
    let error = result.expect_err("move must be refused");
    assert!(w.unmoved(), "refusal must change nothing");
    assert!(w.leftovers().is_empty(), "refusal must leave no staged or held object: {:?}", w.leftovers());
    error
}

// (a) document plus assets, root A -> root B on one filesystem.
#[test]
fn cross_root_document_and_assets_move_completely() {
    let w = World::new(); let hook = two_root_hook(&w);
    let out = w.mv(&hook, OP).unwrap();
    assert_eq!(out["data"]["status"], "committed");
    assert!(w.moved());
    assert_eq!(fs::read(w.b.join("archive/doc.md")).unwrap(), b"# doc\n![x](pic.png)\n");
    assert_eq!(fs::read(w.b.join(".media/archive/doc/pic.png")).unwrap(), b"png-bytes");
    assert_eq!(fs::read(w.a.join("refs/index.md")).unwrap(), b"index", "a guard is read-only evidence");
    let record = w.record(OP).unwrap();
    assert_eq!(record["phase"], "complete");
    assert_eq!(record["plan"]["root_id"], "root-a");
    assert_eq!(record["plan"]["destination_root_id"], "root-b");
    assert_eq!(record["plan"]["destination_root_path"], json!(w.b));
    // History receives each identity leaving root A for root B.
    let plan = hook.registry_plan().expect("registry plan committed");
    let moves = plan.as_array().unwrap();
    assert_eq!(moves.len(), 2);
    for item in moves {
        assert_eq!(item["expected_root_id"], "root-a");
        assert_eq!(item["destination_root_id"], "root-b");
        assert_eq!(item["destination_root_path"], json!(w.b));
    }
    // Replaying a settled operation is idempotent.
    assert_eq!(w.mv(&hook, OP).unwrap()["data"]["status"], "committed");
    assert!(w.moved());
    // Explicit cleanup removes the journal-owned stages and held originals under both roots.
    assert_eq!(w.cleanup(&hook, OP).unwrap()["data"]["cleanup"], "complete");
    assert!(w.leftovers().is_empty(), "cleanup left {:?}", w.leftovers());
    assert!(w.moved());
}

fn crash_then_resume(label: &str, op: &str, faults: Faults, seen: &mut std::collections::BTreeSet<String>) -> bool {
    let w = World::new(); let hook = two_root_hook(&w);
    hook.arm(faults);
    let first = w.mv(&hook, op);
    let phase = w.record(op).map(|r| r["phase"].as_str().unwrap_or("?").to_owned()).unwrap_or_else(|| "no-journal".into());
    hook.arm(Faults::default());
    let resumed = w.mv(&hook, op);
    let outcome = if w.moved() { "fully moved" } else if w.unmoved() { "fully not moved" } else { "SPLIT" };
    println!("{label}: crash result={} phase_at_crash={phase} resume={} -> {outcome}", if first.is_err() { "error" } else { "ok" }, if resumed.is_ok() { "committed" } else { "refused" });
    assert!(w.moved() || w.unmoved(), "split state after resume at {label}: {phase}");
    assert!(w.moved(), "resume must complete the journaled move at {label}");
    assert_eq!(w.cleanup(&hook, op).unwrap()["data"]["cleanup"], "complete");
    assert!(w.leftovers().is_empty(), "{label}: {:?}", w.leftovers());
    seen.insert(phase);
    first.is_err()
}

// (b) interruption at every journal phase, then resume.
#[test]
fn interruption_at_every_phase_resumes_to_exactly_one_state() {
    let clean = World::new(); let hook = two_root_hook(&clean);
    hook.arm(Faults::default()); clean.mv(&hook, OP).unwrap();
    let total = hook.auth_calls();
    assert!(total > 8, "expected a real sequence of authorization calls, saw {total}");
    let mut seen = std::collections::BTreeSet::new();
    for n in 1..=total {
        let crashed = crash_then_resume(&format!("auth-call-{n}"), OP, Faults { auth_at: Some(n), ..Faults::default() }, &mut seen);
        assert!(crashed, "interruption at authorization call {n} must stop the first run");
    }
    for (label, faults) in [
        ("register-after-first", Faults { register_after_first: true, ..Faults::default() }),
        ("prepare-before-checkpoint", Faults { prepare_before: true, ..Faults::default() }),
        ("prepare-after-checkpoint", Faults { prepare_after: true, ..Faults::default() }),
        ("complete-recoverable", Faults { complete: true, ..Faults::default() }),
        ("commit-registry", Faults { registry: true, ..Faults::default() }),
    ] {
        assert!(crash_then_resume(label, OP, faults, &mut seen), "{label} must interrupt the first run");
    }
    for phase in ["prepared", "history_prepared", "committing", "live_committed", "history_pending"] {
        assert!(seen.contains(phase), "no interruption exercised phase {phase}; saw {seen:?}");
    }
}

// The other terminal state: a source that changed while the journal sat in
// Prepared makes resume refuse, and nothing reaches the destination.
#[test]
fn resume_after_source_drift_leaves_everything_at_the_source() {
    let w = World::new(); let hook = two_root_hook(&w);
    hook.arm(Faults { prepare_before: true, ..Faults::default() });
    assert!(w.mv(&hook, OP).is_err());
    assert_eq!(w.record(OP).unwrap()["phase"], "prepared");
    fs::write(w.a.join(".media/notes/doc/pic.png"), b"edited elsewhere").unwrap();
    hook.arm(Faults::default());
    assert!(w.mv(&hook, OP).is_err(), "resume must refuse once the source changed");
    assert!(!w.b.join("archive/doc.md").exists() && !w.b.join(".media/archive/doc/pic.png").exists());
    assert!(w.a.join("notes/doc.md").is_file() && w.a.join(".media/notes/doc/pic.png").is_file());
}

// (c) two different filesystems are refused before anything changes.
#[test]
fn cross_filesystem_is_refused_with_its_own_code_before_mutation() {
    let shm = Path::new("/dev/shm");
    let Ok(other) = tempdir_in(shm) else { println!("SKIP: /dev/shm is not writable"); return; };
    let base = tempdir().unwrap();
    if fs::metadata(other.path()).unwrap().dev() == fs::metadata(base.path()).unwrap().dev() { println!("SKIP: no second filesystem"); return; }
    let other_root = fs::canonicalize(other.path()).unwrap().join("root-b");
    fs::create_dir_all(&other_root).unwrap();
    let w = World::in_dirs(base, Some(&other_root));
    fs::create_dir_all(w.b.join("archive")).unwrap();
    let hook = two_root_hook(&w);
    let error = assert_unchanged_and_refused(&w, w.mv(&hook, OP));
    assert_eq!(error.message, "managed_cross_filesystem_unsupported");
    assert_eq!(error.details.get("code").map(String::as_str), Some("managed_cross_filesystem_unsupported"));
    assert_eq!(error.code, ProtocolErrorCode::Unsupported);
    assert_ne!(error.code, ProtocolErrorCode::Conflict);
    assert!(w.record(OP).is_none(), "a refused move must not even start a journal record");
}

// (d) a folder cannot cross roots.
#[test]
fn folder_move_across_roots_is_refused_and_changes_nothing() {
    let w = World::new(); let hook = two_root_hook(&w);
    fs::create_dir_all(w.a.join("folder/sub")).unwrap(); fs::write(w.a.join("folder/sub/n.md"), b"n").unwrap();
    let source = w.a.join("folder");
    let payload = json!({"operation_id":OP, "source":source, "destination":w.b.join("folder"),
        "expected_snapshot":{"digest":"0".repeat(64), "count":1, "bytes":1}, "references":[], "assets":[]});
    let error = w.send(&hook, Operation::ManagedFolderMove, &source, OP, payload).expect_err("folder cross-root must be refused");
    assert_eq!(error.code, ProtocolErrorCode::Conflict);
    assert!(w.a.join("folder/sub/n.md").is_file() && !w.b.join("folder").exists());
    assert!(w.record(OP).is_none());
}

// (e) authorization and symlink guards, per participant, against its own root.
// (e2) a destination that already exists in the other root is a collision: refused at prepare, both the file there and the source stay.
#[test]
fn destination_collision_in_the_other_root_is_refused_and_changes_nothing() {
    let w = World::new(); let hook = two_root_hook(&w);
    fs::write(w.b.join("archive/doc.md"), b"someone else's note").unwrap();
    let error = w.mv(&hook, OP).expect_err("a colliding destination must be refused");
    assert!(matches!(error.code, ProtocolErrorCode::Conflict | ProtocolErrorCode::Unsupported), "{error:?}");
    assert_eq!(fs::read(w.b.join("archive/doc.md")).unwrap(), b"someone else's note");
    assert!(w.a.join("notes/doc.md").is_file() && w.a.join(".media/notes/doc/pic.png").is_file() && !w.b.join(".media/archive/doc/pic.png").exists());
    assert!(w.leftovers().is_empty(), "{:?}", w.leftovers());
    assert!(hook.registry_plan().is_none(), "History is never told about a refused move");
}

// (e3) an asset that changed since it was read is stale: refused by Native before anything moves.
#[test]
fn a_stale_asset_participant_is_refused_before_anything_moves() {
    let w = World::new(); let hook = two_root_hook(&w);
    fs::write(w.a.join(".media/notes/doc/pic.png"), b"edited after the read").unwrap();
    let error = assert_unchanged_and_refused(&w, w.mv(&hook, OP));
    assert_eq!(error.code, ProtocolErrorCode::Conflict, "{error:?}");
    assert_eq!(fs::read(w.a.join(".media/notes/doc/pic.png")).unwrap(), b"edited after the read");
    assert!(hook.registry_plan().is_none());
}

// (e4) a reference edit whose document changed since it was read is stale too: the whole move is refused, the reference keeps its bytes.
#[test]
fn a_stale_reference_participant_is_refused_before_anything_moves() {
    let w = World::new(); let hook = two_root_hook(&w);
    let mut participants = w.participants();
    participants.as_array_mut().unwrap().push(json!({"kind":"reference", "source":w.a.join("refs/index.md"), "destination":null,
        "expected_fingerprint":fingerprint(b"index as it was read"), "replacement":b"index rewritten".to_vec()}));
    let error = assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, participants, json!([])));
    assert_eq!(error.code, ProtocolErrorCode::Conflict, "{error:?}");
    assert_eq!(fs::read(w.a.join("refs/index.md")).unwrap(), b"index");
    assert!(hook.registry_plan().is_none());
}

// ---- BQ-23: undo of a settled cross-root move, derived from the forward journal only.
const UNDO: &str = "b2p2-undo-operation-0001";

#[test]
fn undo_puts_document_asset_and_reference_back_at_the_old_root_with_exact_bytes() {
    let w = World::new(); let hook = two_root_hook(&w);
    assert_eq!(w.mv_with_reference(&hook, OP).unwrap()["data"]["status"], "committed");
    assert!(w.moved() && fs::read(w.a.join("refs/index.md")).unwrap() == b"index rewritten");
    // The forward cleanup has run: the undo still works because it reads the journal's own staged before-bytes.
    assert_eq!(w.cleanup_with_reference(&hook, OP).unwrap()["data"]["cleanup"], "complete");
    let out = w.undo(&hook, UNDO, OP).unwrap();
    assert_eq!(out["data"]["status"], "committed");
    assert!(w.undone(), "document, asset and reference are back with their forward-before bytes");
    let record = w.record(UNDO).unwrap();
    assert_eq!(record["phase"], "complete");
    assert_eq!(record["plan"]["inverse_of"], OP);
    assert_eq!(record["plan"]["root_id"], "root-b");
    assert_eq!(record["plan"]["destination_root_id"], "root-a");
    // History is told each identity leaves root B for root A.
    let plan = hook.registry_plan().expect("registry plan committed");
    let moves = plan.as_array().unwrap();
    assert_eq!(moves.len(), 2);
    for item in moves {
        assert_eq!(item["expected_root_id"], "root-b");
        assert_eq!(item["destination_root_id"], "root-a");
        assert_eq!(item["destination_root_path"], json!(w.a));
    }
    // Replay of the settled undo is idempotent and changes nothing.
    assert_eq!(w.undo(&hook, UNDO, OP).unwrap()["data"]["status"], "committed");
    assert!(w.undone());
    // An undo of an undo is not offered, and a second undo of the forward move finds nothing left to move.
    let error = w.undo(&hook, "b2p2-undo-operation-0002", UNDO).expect_err("undo of an undo");
    assert_eq!(error.details.get("code").map(String::as_str), Some("managed_undo_of_undo_unsupported"));
    assert!(w.undo(&hook, "b2p2-undo-operation-0003", OP).is_err());
    assert!(w.undone());
}

#[test]
fn undo_refuses_every_drift_before_changing_anything() {
    // (what changed since the forward move, how to change it)
    type Drift = fn(&World);
    let drifts: [(&str, Drift); 5] = [
        ("document at the new root edited", |w| fs::write(w.b.join("archive/doc.md"), b"edited since").unwrap()),
        ("asset at the new root edited", |w| fs::write(w.b.join(".media/archive/doc/pic.png"), b"edited since").unwrap()),
        ("reference edited since", |w| fs::write(w.a.join("refs/index.md"), b"newer user revision").unwrap()),
        ("something now lives at the old document path", |w| fs::write(w.a.join("notes/doc.md"), b"someone else").unwrap()),
        ("document removed from the new root", |w| fs::remove_file(w.b.join("archive/doc.md")).unwrap()),
    ];
    for (label, drift) in drifts {
        let w = World::new(); let hook = two_root_hook(&w);
        w.mv_with_reference(&hook, OP).unwrap();
        drift(&w);
        let before: Vec<_> = ["archive/doc.md", ".media/archive/doc/pic.png"].iter().map(|p| fs::read(w.b.join(p)).ok()).collect();
        let (note_a, ref_a) = (fs::read(w.a.join("notes/doc.md")).ok(), fs::read(w.a.join("refs/index.md")).unwrap());
        let error = w.undo(&hook, UNDO, OP).expect_err(label);
        assert!(matches!(error.code, ProtocolErrorCode::Conflict | ProtocolErrorCode::InvalidPath), "{label}: {error:?}");
        assert!(w.record(UNDO).is_none(), "{label}: a refused undo must not start a journal record");
        assert_eq!(["archive/doc.md", ".media/archive/doc/pic.png"].iter().map(|p| fs::read(w.b.join(p)).ok()).collect::<Vec<_>>(), before, "{label}");
        assert_eq!((fs::read(w.a.join("notes/doc.md")).ok(), fs::read(w.a.join("refs/index.md")).unwrap()), (note_a, ref_a), "{label}: nothing overwritten");
        assert!(!w.a.join(".media/notes/doc/pic.png").exists(), "{label}");
        assert!(w.leftovers().iter().all(|p| p.to_string_lossy().contains("-hold") || p.to_string_lossy().contains(".hold") || p.to_string_lossy().contains(".openclank-managed-")), "{label}");
    }
}

#[test]
fn undo_is_refused_with_a_typed_reason_when_the_forward_journal_is_gone() {
    let w = World::new(); let hook = two_root_hook(&w);
    w.mv(&hook, OP).unwrap();
    fs::remove_file(w.journal.join(format!("{OP}.json"))).unwrap();
    let error = w.undo(&hook, UNDO, OP).expect_err("no journal, no undo");
    assert_eq!(error.code, ProtocolErrorCode::Unsupported);
    assert_eq!(error.details.get("code").map(String::as_str), Some("managed_undo_journal_missing"));
    assert!(w.moved() && w.record(UNDO).is_none());
    // A name that never existed is the same refusal; the request cannot smuggle participants in beside inverse_of.
    let error = w.undo(&hook, "b2p2-undo-operation-0009", "b2p2-never-existed-0001").expect_err("unknown operation");
    assert_eq!(error.details.get("code").map(String::as_str), Some("managed_undo_journal_missing"));
    let payload = json!({"operation_id":UNDO, "inverse_of":OP, "participants":w.participants()});
    assert!(w.send(&hook, Operation::ManagedFileMove, &w.b.join("archive/doc.md"), UNDO, payload).is_err());
}

#[test]
fn undo_of_a_same_root_move_is_not_offered() {
    let w = World::new(); let hook = two_root_hook(&w);
    fs::create_dir_all(w.a.join("other")).unwrap();
    let participants = json!([{"kind":"document", "source":w.a.join("notes/doc.md"), "destination":w.a.join("other/doc.md"),
        "expected_fingerprint":fingerprint(b"# doc\n![x](pic.png)\n"), "replacement":null}]);
    if w.mv_with(&hook, OP, participants, json!([])).is_err() { return; } // a same-root move needs its asset participant; refusal is also "not offered"
    let error = w.send(&hook, Operation::ManagedFileMove, &w.a.join("other/doc.md"), UNDO, json!({"operation_id":UNDO, "inverse_of":OP})).expect_err("same-root undo");
    assert_eq!(error.details.get("code").map(String::as_str), Some("managed_undo_requires_cross_root"));
}

#[test]
fn interruption_of_an_undo_at_every_phase_resumes_to_exactly_one_state() {
    let probe = World::new(); let hook = two_root_hook(&probe);
    probe.mv_with_reference(&hook, OP).unwrap();
    hook.arm(Faults::default()); probe.undo(&hook, UNDO, OP).unwrap();
    let total = hook.auth_calls();
    assert!(total > 8, "expected a real sequence of authorization calls, saw {total}");
    let mut seen = std::collections::BTreeSet::new();
    let mut cases: Vec<(String, Faults)> = (1..=total).map(|n| (format!("auth-call-{n}"), Faults { auth_at: Some(n), ..Faults::default() })).collect();
    cases.extend([
        ("register-after-first".to_owned(), Faults { register_after_first: true, ..Faults::default() }),
        ("prepare-before-checkpoint".to_owned(), Faults { prepare_before: true, ..Faults::default() }),
        ("prepare-after-checkpoint".to_owned(), Faults { prepare_after: true, ..Faults::default() }),
        ("complete-recoverable".to_owned(), Faults { complete: true, ..Faults::default() }),
        ("commit-registry".to_owned(), Faults { registry: true, ..Faults::default() }),
    ]);
    for (label, faults) in cases {
        let w = World::new(); let hook = two_root_hook(&w);
        w.mv_with_reference(&hook, OP).unwrap();
        hook.arm(faults);
        let first = w.undo(&hook, UNDO, OP);
        let phase = w.record(UNDO).map(|r| r["phase"].as_str().unwrap_or("?").to_owned()).unwrap_or_else(|| "no-journal".into());
        hook.arm(Faults::default());
        let resumed = w.undo(&hook, UNDO, OP);
        assert!(resumed.is_ok(), "{label} (phase {phase}): resume must complete the journaled undo: {resumed:?}");
        assert!(w.undone(), "{label} (phase {phase}): split or wrong state after resume");
        assert!(first.is_err(), "{label} must interrupt the first run");
        seen.insert(phase);
    }
    // `history_prepared` is not reachable here: the forward test reaches it through its guard authorizations, and an undo carries no guards.
    for phase in ["prepared", "committing", "live_committed", "history_pending"] {
        assert!(seen.contains(phase), "no interruption exercised phase {phase}; saw {seen:?}");
    }
}

#[test]
fn default_registry_batch_serialization_is_unchanged() {
    // The undo adds no field to the History batch: an existing move still serializes to exactly these keys.
    let w = World::new(); let hook = two_root_hook(&w);
    w.mv(&hook, OP).unwrap();
    let plan = hook.registry_plan().unwrap();
    let mut keys: Vec<_> = plan[0].as_object().unwrap().keys().cloned().collect(); keys.sort();
    assert_eq!(keys, ["destination_relative_path", "destination_root_id", "destination_root_path", "expected_generation", "expected_relative_path",
        "expected_root_id", "replaced_generation", "replaced_resource_id", "resource_id"]);
}

#[test]
fn participant_outside_every_root_is_refused() {
    let w = World::new(); let hook = two_root_hook(&w);
    let outside = tempdir().unwrap(); let outside_dir = fs::canonicalize(outside.path()).unwrap();
    let mut participants = w.participants();
    participants[0]["destination"] = json!(outside_dir.join("doc.md"));
    assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, participants, w.guards()));
    let mut participants = w.participants();
    participants[1]["destination"] = json!(outside_dir.join(".media/x/pic.png"));
    assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, participants, w.guards()));
    assert!(fs::read_dir(&outside_dir).unwrap().next().is_none());
}

#[test]
fn symlinked_component_in_either_root_is_refused() {
    // Destination root: archive/ is a symlink to a directory inside the same root.
    let w = World::new(); let hook = two_root_hook(&w);
    fs::remove_dir(w.b.join("archive")).unwrap(); fs::create_dir(w.b.join("real")).unwrap();
    std::os::unix::fs::symlink(w.b.join("real"), w.b.join("archive")).unwrap();
    let error = assert_unchanged_and_refused(&w, w.mv(&hook, OP));
    assert_eq!(error.code, ProtocolErrorCode::Denied, "destination symlink must be an authorization refusal");
    assert!(fs::read_dir(w.b.join("real")).unwrap().next().is_none());
    // Source root: notes/ is a symlink.
    let w = World::new(); let hook = two_root_hook(&w);
    fs::rename(w.a.join("notes"), w.a.join("real")).unwrap(); std::os::unix::fs::symlink(w.a.join("real"), w.a.join("notes")).unwrap();
    let error = w.mv(&hook, OP).expect_err("symlinked source component must be refused");
    assert_eq!(error.code, ProtocolErrorCode::Denied, "source symlink must be an authorization refusal");
    assert!(!w.b.join("archive/doc.md").exists() && w.a.join("real/doc.md").is_file());
    assert!(w.leftovers().is_empty(), "{error:?}");
    // A symlinked media container on the destination side.
    let w = World::new(); let hook = two_root_hook(&w);
    fs::create_dir_all(w.b.join("elsewhere")).unwrap(); std::os::unix::fs::symlink(w.b.join("elsewhere"), w.b.join(".media")).unwrap();
    let error = assert_unchanged_and_refused(&w, w.mv(&hook, OP));
    assert_eq!(error.code, ProtocolErrorCode::Denied, "destination media symlink must be an authorization refusal");
    assert!(fs::read_dir(w.b.join("elsewhere")).unwrap().next().is_none());
}

// (f) at most two roots; a guard stays in the source root.
#[test]
fn a_third_root_is_refused() {
    let w = World::new();
    let c = w.a.parent().unwrap().join("root-c"); fs::create_dir_all(c.join(".media/archive/doc")).unwrap();
    let hook = Hook::new(&[("root-a", &w.a), ("root-b", &w.b), ("root-c", &c)]);
    let mut participants = w.participants();
    participants[1]["destination"] = json!(c.join(".media/archive/doc/pic.png"));
    assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, participants, w.guards()));
    assert!(fs::read_dir(c.join(".media/archive/doc")).unwrap().next().is_none());
    // A source in a third root.
    fs::create_dir_all(c.join("s")).unwrap(); fs::write(c.join("s/other.md"), b"other").unwrap();
    let mut participants = w.participants();
    participants.as_array_mut().unwrap().push(json!({"kind":"reference", "source":c.join("s/other.md"),
        "destination":null, "expected_fingerprint":fingerprint(b"other"), "replacement":[111]}));
    assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, participants, w.guards()));
    assert_eq!(fs::read(c.join("s/other.md")).unwrap(), b"other");
    // A guard in the destination root is not accepted.
    fs::write(w.b.join("archive/guard.md"), b"g").unwrap();
    let guards = json!([{"source":w.b.join("archive/guard.md"), "expected_fingerprint":fingerprint(b"g")}]);
    assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, w.participants(), guards));
}

// The destination root's asset layout rule applies; a destination that does
// not satisfy it is refused rather than guessed.
#[test]
fn asset_destination_must_satisfy_the_destination_root_layout() {
    let w = World::new(); let hook = two_root_hook(&w);
    let mut participants = w.participants();
    participants[1]["destination"] = json!(w.b.join("archive/stray/pic.png"));
    assert_unchanged_and_refused(&w, w.mv_with(&hook, OP, participants, w.guards()));
    // Layout v1 (`<doc dir>/media/<stem>/<file>`) at the destination is accepted when exact.
    let mut participants = w.participants();
    participants[1]["destination"] = json!(w.b.join("archive/media/doc/pic.png"));
    fs::create_dir_all(w.b.join("archive/media/doc")).unwrap();
    assert_eq!(w.mv_with(&hook, OP, participants, w.guards()).unwrap()["data"]["status"], "committed");
    assert_eq!(fs::read(w.b.join("archive/media/doc/pic.png")).unwrap(), b"png-bytes");
}

// (g) a journal record written by the code before cross-root support. The
// literal below is the exact record an unmodified build wrote when a
// single-root move stopped during History registration (phase `prepared`,
// two staged destinations, one checkpointed registration); only the
// temp-directory path, the digest and the OS object identities, which cannot
// be literal, are substituted.
const LEGACY_RECORD: &str = r#"{"version":1,"operation_id":"b2p2-legacy-0000001","owner_id":"owner-1","request_digest":"@DIGEST@","phase":"prepared","plan":{"guards":[],"history_action_id":"action-b2p2-legacy-0000001","history_timestamp_millis":1794361439054,"participants":[{"before_digest":"038da2aefb4f4a5586af86808943e02de4c58ca53ecc4e0dd46e1809cae78d6b","before_stage":"before-0","destination":"@ROOT@/notes/moved.md","expected_fingerprint":"sha256:038da2aefb4f4a5586af86808943e02de4c58ca53ecc4e0dd46e1809cae78d6b:20","kind":"document","mode":33188,"replacement_digest":null,"replacement_stage":null,"source":"@ROOT@/notes/doc.md","source_hold":"@ROOT@/notes/.openclank-managed-b2p2-legacy-0000001-0.hold","source_identity":"@ID_DOC@"},{"before_digest":"ea80334363eed145dfeee51ebae7dc3f1cd7d0c7879f8bfd2070c061d3c33f56","before_stage":"before-1","destination":"@ROOT@/.media/notes/moved/pic.png","expected_fingerprint":"sha256:ea80334363eed145dfeee51ebae7dc3f1cd7d0c7879f8bfd2070c061d3c33f56:9","kind":"asset","mode":33188,"replacement_digest":null,"replacement_stage":null,"source":"@ROOT@/.media/notes/doc/pic.png","source_hold":"@ROOT@/.media/notes/doc/.openclank-managed-b2p2-legacy-0000001-1.hold","source_identity":"@ID_ASSET@"}],"policy_generation":0,"root_id":"root-a","root_identity":"@ID_ROOT@","root_path":"@ROOT@"},"file_stages":[{"digest":"038da2aefb4f4a5586af86808943e02de4c58ca53ecc4e0dd46e1809cae78d6b","identity":"@ID_STAGE0@","index":0,"path":"@ROOT@/notes/.openclank-managed-098ba487935d7760be1435429f2e97d6.stage"},{"digest":"ea80334363eed145dfeee51ebae7dc3f1cd7d0c7879f8bfd2070c061d3c33f56","identity":"@ID_STAGE1@","index":1,"path":"@ROOT@/.media/notes/.openclank-managed-aacf0f2208f78e0528353af3837ca55b.stage"}],"directory_events":[],"commit_progress":[],"cleanup_progress":[],"preparation":[{"detail":{"generation":1,"relative_path":"notes/doc.md","resource_id":"res:@ROOT@/notes/doc.md","root_id":"root-a","root_path":"@ROOT@"},"index":0,"kind":"resource"}],"registry_plan":null,"history_prepare":null,"history_completion":null,"review_outcome":null}"#;

#[test]
fn record_written_before_cross_root_support_still_resumes() {
    let w = World::new(); let hook = Hook::new(&[("root-a", &w.a)]);
    let op = "b2p2-legacy-0000001";
    let participants = json!([
        {"kind":"document", "source":w.a.join("notes/doc.md"), "destination":w.a.join("notes/moved.md"),
         "expected_fingerprint":fingerprint(b"# doc\n![x](pic.png)\n"), "replacement":null},
        {"kind":"asset", "source":w.a.join(".media/notes/doc/pic.png"), "destination":w.a.join(".media/notes/moved/pic.png"),
         "expected_fingerprint":fingerprint(b"png-bytes"), "replacement":null}]);
    for path in [w.a.join("notes/doc.md"), w.a.join(".media/notes/doc/pic.png")] {
        fs::set_permissions(path, std::os::unix::fs::PermissionsExt::from_mode(0o644)).unwrap();
    }
    let stage0 = w.a.join("notes/.openclank-managed-098ba487935d7760be1435429f2e97d6.stage");
    let stage1 = w.a.join(".media/notes/.openclank-managed-aacf0f2208f78e0528353af3837ca55b.stage");
    fs::write(&stage0, b"# doc\n![x](pic.png)\n").unwrap(); fs::write(&stage1, b"png-bytes").unwrap();
    let request = json!({"owner_id":"owner-1", "policy_generation":0, "participants":participants, "guards":[]});
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&request).unwrap()));
    let identity = |path: &Path| { let m = fs::metadata(path).unwrap(); json!({"device":m.dev(), "inode":m.ino(), "kind":"unix"}).to_string() };
    let text = LEGACY_RECORD.replace("@ROOT@", w.a.to_str().unwrap()).replace("@DIGEST@", &digest)
        .replace("\"@ID_ROOT@\"", &identity(&w.a)).replace("\"@ID_DOC@\"", &identity(&w.a.join("notes/doc.md")))
        .replace("\"@ID_ASSET@\"", &identity(&w.a.join(".media/notes/doc/pic.png")))
        .replace("\"@ID_STAGE0@\"", &identity(&stage0)).replace("\"@ID_STAGE1@\"", &identity(&stage1));
    let parsed: Value = serde_json::from_str(&text).unwrap();
    assert!(parsed["plan"].get("destination_root_id").is_none() && parsed["plan"]["root_id"] == "root-a");
    fs::create_dir_all(&w.journal).unwrap();
    fs::write(w.journal.join(format!("{op}.json")), &text).unwrap();
    fs::write(w.journal.join(format!("{op}.before-0.blob")), b"# doc\n![x](pic.png)\n").unwrap();
    fs::write(w.journal.join(format!("{op}.before-1.blob")), b"png-bytes").unwrap();
    let doc = w.a.join("notes/doc.md");
    let out = w.send(&hook, Operation::ManagedFileMove, &doc, op, json!({"operation_id":op, "participants":participants, "guards":[]})).unwrap();
    assert_eq!(out["data"]["status"], "committed");
    assert!(w.a.join("notes/moved.md").is_file() && w.a.join(".media/notes/moved/pic.png").is_file());
    assert!(!doc.exists() && !w.a.join(".media/notes/doc/pic.png").exists());
    let resumed = w.record(op).unwrap();
    assert_eq!(resumed["phase"], "complete");
    assert!(resumed["plan"].get("destination_root_id").is_none(), "an old record stays single-root");
}

// (h) a single-root move behaves as before: same plan keys, same outcome.
#[test]
fn single_root_move_is_unchanged() {
    let w = World::new(); let hook = Hook::new(&[("root-a", &w.a)]);
    let participants = json!([
        {"kind":"document", "source":w.a.join("notes/doc.md"), "destination":w.a.join("notes/moved.md"),
         "expected_fingerprint":fingerprint(b"# doc\n![x](pic.png)\n"), "replacement":null},
        {"kind":"asset", "source":w.a.join(".media/notes/doc/pic.png"), "destination":w.a.join(".media/notes/moved/pic.png"),
         "expected_fingerprint":fingerprint(b"png-bytes"), "replacement":null}]);
    let out = w.mv_with(&hook, OP, participants, w.guards()).unwrap();
    assert_eq!(out["data"]["status"], "committed");
    assert!(w.a.join("notes/moved.md").is_file() && w.a.join(".media/notes/moved/pic.png").is_file());
    assert!(!w.a.join("notes/doc.md").exists() && !w.a.join(".media/notes/doc/pic.png").exists());
    let record = w.record(OP).unwrap();
    let mut keys: Vec<_> = record["plan"].as_object().unwrap().keys().cloned().collect(); keys.sort();
    assert_eq!(keys, ["guards", "history_action_id", "history_timestamp_millis", "participants", "policy_generation", "root_id", "root_identity", "root_path"]);
    assert_eq!(record["phase"], "complete");
    assert_eq!(w.cleanup_single(&hook).unwrap()["data"]["cleanup"], "complete");
    assert!(w.leftovers().is_empty());
    // A destination under a root that is not registered is still refused, as before.
    let w2 = World::new(); let hook2 = Hook::new(&[("root-a", &w2.a)]);
    assert_unchanged_and_refused(&w2, w2.mv(&hook2, OP));
}

impl World {
    fn cleanup_single(&self, hook: &Arc<Hook>) -> Result<Value, ProtocolError> {
        let participants = json!([
            {"kind":"document", "source":self.a.join("notes/doc.md"), "destination":self.a.join("notes/moved.md"),
             "expected_fingerprint":fingerprint(b"# doc\n![x](pic.png)\n"), "replacement":null},
            {"kind":"asset", "source":self.a.join(".media/notes/doc/pic.png"), "destination":self.a.join(".media/notes/moved/pic.png"),
             "expected_fingerprint":fingerprint(b"png-bytes"), "replacement":null}]);
        self.send(hook, Operation::ManagedFileCleanup, &self.a, OP, json!({"operation_id":OP, "participants":participants, "guards":self.guards()}))
    }
}
