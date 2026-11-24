//! S07 source-authored calibration; no measurement until root releases a slot.
//! Child of reuse_tests so the existing isolated authority and canonical oracle
//! remain authoritative. Never invoke this by a broad ignored-test selector.
use super::*;
use serde_json::{json, Value};
use std::io::Write;
use std::time::Instant;

const SCHEMA: &str = "open-clank.s07-native-calibration/v1";
const RECIPE: &str = "mixed-192-v1-64-functions-128-literal-arithmetic-terms";
const PREP: &str = "s07-prepared.json";
const ORDER: [[usize; 3]; 3] = [[1, 2, 4], [2, 4, 1], [4, 1, 2]];

/// Exactly 192 eligible-language files: 64 Python (including the original
/// five fixture files), 64 Rust, 64 TypeScript. One unsupported asset is extra.
/// Arithmetic-heavy sources avoid a giant all-to-all reference graph. This is
/// parser-heavy by construction, NOT a claim that total publication is CPU-bound.
fn recipe() -> BTreeMap<String, Vec<u8>> {
    let mut files = BTreeMap::from([
        ("lib.py".into(), b"def target(): return 1\n".to_vec()),
        ("caller.py".into(), b"from lib import target, future_name\ndef caller():\n    target()\n    return future_name()\n".to_vec()),
        ("empty.py".into(), b"# valid zero source units\n".to_vec()),
        ("broken.py".into(), b"def broken(:\n".to_vec()),
        ("binary.py".into(), b"\0binary".to_vec()),
        ("disconnected.ts".into(), b"export const isolated = 7;\n".to_vec()),
        ("asset.dat".into(), b"unsupported synthetic asset\n".to_vec()),
    ]);
    // Literal arithmetic makes a substantial syntax tree without creating
    // millions of value-reference evidence records or exceeding old oracles.
    let expression = (0..128)
        .map(|n| format!("({})", n % 10))
        .collect::<Vec<_>>()
        .join(" + ");
    for (extension, count) in [("py", 59), ("rs", 64), ("ts", 63)] {
        for file in 0..count {
            let mut text = String::new();
            for function in 0..64 {
                let name = format!("s07_{extension}_{file:02}_{function:03}");
                match extension {
                    "py" => text.push_str(&format!("def {name}(x):\n    return {expression}\n\n")),
                    "rs" => text.push_str(&format!(
                        "pub fn {name}(x: i64) -> i64 {{ {expression} }}\n"
                    )),
                    "ts" => text.push_str(&format!(
                        "export function {name}(x: number): number {{ return {expression}; }}\n"
                    )),
                    _ => unreachable!(),
                }
            }
            assert!((48 * 1024..96 * 1024).contains(&text.len()));
            assert!(files
                .insert(format!("load_{file:02}.{extension}"), text.into_bytes())
                .is_none());
        }
    }
    assert_eq!(files.len(), 193);
    assert_eq!(files.values().map(Vec::len).sum::<usize>(), 9_656_852);
    files
}

fn hash(value: &impl serde::Serialize) -> String {
    blake3::hash(&serde_json::to_vec(value).unwrap())
        .to_hex()
        .to_string()
}

fn source_inventory(files: &BTreeMap<String, Vec<u8>>) -> Value {
    let rows: Vec<_> = files.iter().map(|(path, bytes)| {
        json!({"path":path,"bytes":bytes.len(),"blake3":blake3::hash(bytes).to_hex().to_string()})
    }).collect();
    json!({"recipe":RECIPE,"files":rows,"total_bytes":files.values().map(Vec::len).sum::<usize>()})
}

fn assert_source(fixture: &ReuseFixture, prepared: &Value) {
    let expected = recipe();
    assert_eq!(source_inventory(&expected), prepared["source_inventory"]);
    let mut names = Vec::new();
    for entry in std::fs::read_dir(&fixture.binding.root).unwrap() {
        let entry = entry.unwrap();
        assert!(entry.file_type().unwrap().is_file());
        let name = entry.file_name().into_string().unwrap();
        assert_eq!(std::fs::read(entry.path()).unwrap(), expected[&name]);
        names.push(name);
    }
    names.sort();
    assert_eq!(names, expected.keys().cloned().collect::<Vec<_>>());
}

fn exclusive(path: &Path, value: &Value) {
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    file.write_all(&serde_json::to_vec(value).unwrap()).unwrap();
    file.sync_all().unwrap();
}

fn read_json(path: &Path) -> Value {
    let metadata = std::fs::symlink_metadata(path).unwrap();
    assert!(
        metadata.is_file() && !metadata.file_type().is_symlink() && metadata.len() <= 1024 * 1024
    );
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

fn record_hash(
    evidence: &FileEvidence,
    graph: &crate::graph::GraphUpsertInput,
    resolutions: &mut [ResolutionEvidence],
) -> String {
    resolutions.sort_by(|a, b| a.occurrence_id.cmp(&b.occurrence_id));
    // All IDs, source locators, properties and original relationship states
    // retained. Only resolution ordering is canonicalized on both sides.
    hash(&json!({"evidence":evidence,"graph":graph,"resolutions":resolutions}))
}

/// Independent serial extraction/resolution, run ONLY during unmeasured prepare.
/// The frozen expected output exists before the first timed native index.
fn oracle(fixture: &ReuseFixture, checkout: &str) -> Value {
    let identity = directory_identity(&fixture.binding.root).unwrap();
    let scan = scan_codebase_pinned(&fixture.binding.root, &identity).unwrap();
    assert_eq!(scan.files.len(), 192);
    let mut clean = BTreeMap::new();
    let mut unavailable = Vec::new();
    let mut budget = ExtractionBudget::default();
    budget.max_serialized_bytes = storage::MAX_DECODED;
    for path in scan.files {
        let relative = path
            .strip_prefix(&fixture.binding.root)
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        let acquired = acquire_source_pinned(&fixture.binding.root, &path, &identity).unwrap();
        match index_acquired_budget(checkout, &fixture.binding.root, &path, &acquired, &budget) {
            Ok(file) => {
                clean.insert(relative, file);
            }
            Err(error) => unavailable.push(UnavailableFile {
                rel_path: relative,
                source_hash: Some(blake3::hash(&acquired.bytes).to_hex().to_string()),
                reason: if error == "binary source" {
                    "binary"
                } else {
                    "parse_failed"
                }
                .into(),
            }),
        }
    }
    assert_eq!(clean.len(), 190);
    assert_eq!(unavailable.len(), 2);
    let resolved = resolve_evidence(
        &CorpusInput {
            checkout_id: checkout.into(),
            files: clean.values().map(|f| f.evidence.clone()).collect(),
            unavailable_files: unavailable,
        },
        &ResolutionBudget::default(),
    )
    .unwrap();
    // Prove a real cross-file relationship, and a genuinely disconnected file.
    let caller = &clean["caller.py"].evidence;
    let target = &clean["lib.py"].evidence;
    assert!(resolved
        .resolutions
        .iter()
        .any(|r| r.state == ResolutionState::Exact
            && caller
                .declarations
                .iter()
                .any(|d| Some(&d.id) == r.caller_id.as_ref())
            && target
                .declarations
                .iter()
                .any(|d| d.name == "target" && Some(&d.id) == r.target_id.as_ref())));
    assert!(clean["disconnected.ts"].evidence.occurrences.is_empty());
    let mut records = BTreeMap::new();
    for (path, file) in clean {
        let mut relationships: Vec<_> = resolved
            .resolutions
            .iter()
            .filter(|r| r.source.rel_path == path)
            .cloned()
            .collect();
        records.insert(
            path,
            record_hash(&file.evidence, &file.upsert, &mut relationships),
        );
    }
    json!({"records":records,"canonical_blake3":hash(&records),"successful_files":190,"errors":2})
}

fn actual_records(fixture: &ReuseFixture, store: &SqliteStore) -> Value {
    let generation = fixture.generation(store);
    let ids: Vec<i64> = generation
        .db
        .prepare("SELECT id FROM files ORDER BY path")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    let mut records = BTreeMap::new();
    // One file at a time, outside timing and outside the phase RSS snapshot;
    // no whole-corpus retained JSON copy for the validation oracle.
    for id in ids {
        let file = generation.load(id).unwrap();
        let graph: Vec<u8> = generation
            .db
            .query_row("SELECT graph FROM files WHERE id=?1", params![id], |r| {
                r.get(0)
            })
            .unwrap();
        let graph: crate::graph::GraphUpsertInput = storage::decode(&graph).unwrap();
        let mut resolutions = generation.resolutions(id).unwrap();
        records.insert(
            file.rel_path.clone(),
            record_hash(&file, &graph, &mut resolutions),
        );
    }
    json!({"records":records,"canonical_blake3":hash(&records),"successful_files":190,"errors":2})
}

fn prepared_fixture(root: &Path, prepared: &Value, home: &str) -> ReuseFixture {
    assert_eq!(prepared["schema"], SCHEMA);
    assert_eq!(prepared["producer"], crate::code::producer_fingerprint());
    let binding: WorkspaceBinding = serde_json::from_value(prepared["binding"].clone()).unwrap();
    assert_eq!(binding.root, root.join("source"));
    let previous = ["HOME", "OPEN_CLANK_AUTHORITY_DB_PATH"]
        .into_iter()
        .map(|key| (key, std::env::var_os(key)))
        .collect();
    std::env::set_var("HOME", root.join(home));
    std::env::set_var(
        "OPEN_CLANK_AUTHORITY_DB_PATH",
        root.join("authority.sqlite"),
    );
    ReuseFixture {
        root: root.into(),
        previous,
        scope: GraphScope::new(&binding.owner, &binding.workspace_id).unwrap(),
        binding,
        preserve: true,
    }
}

#[derive(Clone, Copy)]
struct Usage {
    user_ns: u64,
    system_ns: u64,
    peak_bytes: u64,
}
fn usage() -> Usage {
    let mut value = std::mem::MaybeUninit::<libc::rusage>::uninit();
    assert_eq!(
        unsafe { libc::getrusage(libc::RUSAGE_SELF, value.as_mut_ptr()) },
        0
    );
    let value = unsafe { value.assume_init() };
    let ns = |time: libc::timeval| {
        u64::try_from(time.tv_sec).unwrap() * 1_000_000_000
            + u64::try_from(time.tv_usec).unwrap() * 1000
    };
    #[cfg(target_os = "macos")]
    let peak_bytes = u64::try_from(value.ru_maxrss).unwrap();
    #[cfg(not(target_os = "macos"))]
    let peak_bytes = u64::try_from(value.ru_maxrss)
        .unwrap()
        .checked_mul(1024)
        .unwrap();
    Usage {
        user_ns: ns(value.ru_utime),
        system_ns: ns(value.ru_stime),
        peak_bytes,
    }
}

/// First, unmeasured. Prints the private synthetic root so root can pass it to
/// subsequent processes. Creates no native index and never touches live stores.
#[test]
#[ignore = "root-serialized S07 preparation only; exact selector and explicit admission"]
fn s07_prepare_frozen_recipe() {
    assert_eq!(
        std::env::var("OPENCLANK_S07_ADMISSION").unwrap(),
        "prepare-mixed-192-v1"
    );
    let mut fixture = ReuseFixture::new(0);
    fixture.preserve = true; // Preserve failures and prepared artifacts as well.
    let files = recipe();
    for (name, bytes) in &files {
        std::fs::write(fixture.binding.root.join(name), bytes).unwrap();
    }
    let seed = fixture.root.join("s07-registered-seed.sqlite");
    let state = {
        let store = SqliteStore::new(seed.to_str().unwrap(), 4).unwrap();
        store
            .code_workspace_prepare(&fixture.scope, &fixture.binding, false)
            .unwrap()
    };
    assert!(
        !seed.with_extension("sqlite-wal").exists(),
        "seed must be closed/checkpointed"
    );
    let expected = oracle(&fixture, state["checkout_id"].as_str().unwrap());
    let prepared = json!({"schema":SCHEMA,"recipe":RECIPE,"producer":crate::code::producer_fingerprint(),
        "source_inventory":source_inventory(&files),"source_inventory_blake3":hash(&source_inventory(&files)),
        "binding":fixture.binding,"checkout_id":state["checkout_id"],"seed_blake3":blake3::hash(&std::fs::read(seed).unwrap()).to_hex().to_string(),
        "oracle":expected,"balanced_orders":ORDER});
    assert_source(&fixture, &prepared);
    exclusive(&fixture.root.join(PREP), &prepared);
    println!(
        "S07_PREPARED {}",
        json!({"schema":SCHEMA,"root":fixture.root,"recipe":RECIPE,
        "source_inventory_blake3":prepared["source_inventory_blake3"],"bytes":prepared["source_inventory"]["total_bytes"],
        "eligible_files":192,"successful_files":190,"errors":2,"canonical_blake3":prepared["oracle"]["canonical_blake3"],
        "preparation_is_unmeasured":true})
    );
}

/// Exactly one phase in one fresh process. Root schedules each round in ORDER;
/// cold then unchanged for each setting, never parallel measurement processes.
#[test]
#[ignore = "root-serialized S07 measurement only; fresh process per setting/phase"]
fn s07_measure_frozen_recipe_phase() {
    assert_eq!(
        std::env::var("OPENCLANK_S07_ADMISSION").unwrap(),
        "measure-mixed-192-v1"
    );
    let supplied = PathBuf::from(std::env::var_os("OPENCLANK_S07_ROOT").unwrap());
    assert!(!std::fs::symlink_metadata(&supplied)
        .unwrap()
        .file_type()
        .is_symlink());
    let root = supplied.canonicalize().unwrap();
    assert_eq!(
        root.parent().unwrap(),
        std::env::temp_dir().canonicalize().unwrap()
    );
    assert!(root
        .file_name()
        .unwrap()
        .to_str()
        .unwrap()
        .starts_with("fm-partition-reuse-"));
    let threads: usize = std::env::var("OPENCLANK_S07_THREADS")
        .unwrap()
        .parse()
        .unwrap();
    let repetition: usize = std::env::var("OPENCLANK_S07_REPETITION")
        .unwrap()
        .parse()
        .unwrap();
    let phase = std::env::var("OPENCLANK_S07_PHASE").unwrap();
    assert!([1, 2, 4].contains(&threads) && repetition < ORDER.len());
    assert!(["cold", "unchanged"].contains(&phase.as_str()));
    // Fixed balanced process order, no post-hoc ordering or retries. Each
    // configuration's unchanged phase must finish before the next cold run.
    let position = ORDER[repetition]
        .iter()
        .position(|&n| n == threads)
        .unwrap();
    if phase == "cold" && (repetition > 0 || position > 0) {
        let (prior_round, prior_threads) = if position > 0 {
            (repetition, ORDER[repetition][position - 1])
        } else {
            (repetition - 1, ORDER[repetition - 1][2])
        };
        let previous = read_json(&root.join(format!(
            "s07-r{prior_round}-t{prior_threads}-unchanged.json"
        )));
        assert_eq!(previous["status"], "passed");
    }
    let prepared = read_json(&root.join(PREP));
    let home = format!("home-r{repetition}-t{threads}");
    let fixture = prepared_fixture(&root, &prepared, &home);
    assert_source(&fixture, &prepared);
    let prefix = format!("r{repetition}-t{threads}");
    let database = root.join(format!("s07-{prefix}.sqlite"));
    let cold_receipt = root.join(format!("s07-{prefix}-cold.json"));
    let cold = if phase == "cold" {
        assert!(!database.exists() && !root.join(&home).exists());
        let seed = std::fs::read(root.join("s07-registered-seed.sqlite")).unwrap();
        assert_eq!(
            blake3::hash(&seed).to_hex().as_str(),
            prepared["seed_blake3"].as_str().unwrap()
        );
        std::fs::create_dir(root.join(&home)).unwrap();
        use std::os::unix::fs::OpenOptionsExt;
        let mut destination = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&database)
            .unwrap();
        destination.write_all(&seed).unwrap();
        destination.sync_all().unwrap();
        None
    } else {
        let receipt = read_json(&cold_receipt);
        assert_eq!(receipt["status"], "passed");
        assert_eq!(receipt["producer"], prepared["producer"]);
        assert_eq!(
            receipt["source_inventory_blake3"],
            prepared["source_inventory_blake3"]
        );
        assert!(root.join(&home).is_dir() && database.is_file());
        Some(receipt)
    };
    // Consume-once intent remains on any failure. No retry/overwrite/resume.
    exclusive(
        &root.join(format!("s07-{prefix}-{phase}.intent.json")),
        &json!({"schema":SCHEMA,"phase":phase,"threads":threads,"repetition":repetition,"prepared_blake3":hash(&prepared)}),
    );
    let store = SqliteStore::new(database.to_str().unwrap(), 4).unwrap();
    let scheduler = store.index_scheduler();
    scheduler.configure(1, threads).unwrap();
    assert_eq!(scheduler.status().effective, threads);
    scheduler.test_observation(true);
    let prior = cold.as_ref().map(|_| {
        store
            .code_evidence_receipt(&fixture.scope, &fixture.binding)
            .unwrap()
    });
    let before = usage();
    let started = Instant::now();
    let (result, metrics) = fixture.index(&store);
    let wall_ns = started.elapsed().as_nanos();
    let after = usage(); // BEFORE oracle reads/decoding, not a resettable RSS.
    let observation = scheduler.test_observation(false);
    assert!(observation.cpu_peak > 0 && observation.cpu_peak <= threads);
    assert!(observation.worker_peak <= threads && observation.worker_active == 0);
    assert_eq!(scheduler.status().active, 0);
    assert_eq!(scheduler.status().queued, 0);
    assert_eq!(scheduler.test_accounting(), (0, 0, 0, 0));
    assert_eq!(result.codebase, prepared["checkout_id"].as_str().unwrap());
    assert_eq!(result.errors.len(), 2);
    assert_eq!(result.files_indexed + result.files_unchanged, 190);
    assert_eq!(result.files_removed, 0);
    let receipt = store
        .code_evidence_receipt(&fixture.scope, &fixture.binding)
        .unwrap();
    if let Some(prior) = prior {
        assert_eq!(receipt, prior);
        assert_eq!((result.files_indexed, result.files_unchanged), (0, 190));
        for stage in [
            Stage::Extract,
            Stage::Resolve,
            Stage::Stage,
            Stage::Seal,
            Stage::Publish,
        ] {
            assert_eq!(calls(&metrics, stage), 0);
        }
        assert_eq!(observation.worker_attempts, 0);
    } else {
        assert_eq!((result.files_indexed, result.files_unchanged), (190, 0));
        assert_eq!(calls(&metrics, Stage::Extract), 192);
        assert_eq!(observation.worker_attempts, 192);
        for stage in [Stage::Resolve, Stage::Stage, Stage::Seal, Stage::Publish] {
            assert!(calls(&metrics, stage) > 0);
        }
    }
    assert_eq!(actual_records(&fixture, &store), prepared["oracle"]);
    assert!(fixture.exact_target(&store, "target"));
    assert_source(&fixture, &prepared);
    let mut histogram = BTreeMap::new();
    for row in &result.coverage {
        let language = match Path::new(&row.rel_path)
            .extension()
            .and_then(|s| s.to_str())
        {
            Some("py") => "python",
            Some("rs") => "rust",
            Some("ts") => "typescript",
            _ => "other",
        };
        *histogram
            .entry(format!("{}:{language}", row.status))
            .or_insert(0usize) += 1;
    }
    let output = json!({"schema":SCHEMA,"status":"passed","recipe":RECIPE,"phase":phase,"threads":threads,"repetition":repetition,
        "producer":prepared["producer"],"source_inventory_blake3":prepared["source_inventory_blake3"],"prepared_blake3":hash(&prepared),
        "canonical_blake3":prepared["oracle"]["canonical_blake3"],"receipt_blake3":hash(&receipt),
        "wall_ns":wall_ns,"user_ns":after.user_ns-before.user_ns,"system_ns":after.system_ns-before.system_ns,
        "phase_rss_high_water_bytes":after.peak_bytes,"rss_includes_process_setup":true,"process_time_l_also_includes_post_validation":true,
        "metrics":metrics,"scheduler":scheduler.status(),"transition_observation":observation,
        "indexed":result.files_indexed,"unchanged":result.files_unchanged,"removed":result.files_removed,"errors":result.errors.len(),
        "coverage_entries":result.coverage.len(),"status_extension_language":histogram,
        "numeric_data_credits_are_not_heap_or_rss":true,"small_snapshot_lane_is_serial":true});
    exclusive(&root.join(format!("s07-{prefix}-{phase}.json")), &output);
    println!("S07_MEASUREMENT {}", output);
}

#[test]
fn s07_transition_observer_counts_real_workers_and_resets_only_drained() {
    use crate::code_index_scheduler::IndexScheduler;
    use std::sync::{Arc, Barrier};
    for count in [1, 2, 4] {
        let scheduler = Arc::new(IndexScheduler::default());
        scheduler.configure(1, count).unwrap();
        let barrier = Arc::new(Barrier::new(count));
        scheduler.test_observation(true);
        scheduler
            .run(|| {
                assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(
                    || scheduler.test_observation(true)
                ))
                .is_err());
                let mut handles = Vec::new();
                for _ in 0..count {
                    let barrier = barrier.clone();
                    handles.push(scheduler.submit(scheduler.data(1)?, move || {
                        barrier.wait();
                        Ok(())
                    })?);
                }
                for handle in handles {
                    drop(handle.wait()?);
                }
                Ok(())
            })
            .unwrap();
        let observation = scheduler.test_observation(true);
        assert_eq!(observation.cpu_peak, count);
        assert_eq!(observation.worker_peak, count);
        assert_eq!(observation.worker_attempts, count);
        assert_eq!(observation.worker_active, 0);
        assert_eq!(scheduler.test_observation(false).cpu_peak, 0);
        assert_eq!(scheduler.test_accounting(), (0, 0, 0, 0));
    }
}

#[test]
fn s07_recipe_has_fixed_bytes_membership_and_language_distribution() {
    let files = recipe();
    for extension in ["py", "rs", "ts"] {
        assert_eq!(
            files
                .keys()
                .filter(|p| p.ends_with(&format!(".{extension}")))
                .count(),
            64
        );
    }
    assert_eq!(files.keys().filter(|p| p.ends_with(".dat")).count(), 1);
    assert!(files.values().all(|bytes| bytes.len() < 512 * 1024));
    assert!(files
        .iter()
        .filter(|(path, _)| path.starts_with("load_"))
        .all(|(_, bytes)| bytes.iter().filter(|&&b| b == b'\n').count() >= 64));
}
