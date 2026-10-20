//! Scan-root reuse safety/parity; synthetic files only. No corpus admission.
use super::*;
use std::os::unix::fs::symlink;

// Only tests can force the pre-change scan-local component walker. This is
// deliberately not the older repeated-absolute-root scanner oracle.
struct ComponentWalkerGuard(bool);
impl ComponentWalkerGuard {
    fn force() -> Self {
        Self(SCAN_FORCE_COMPONENT_WALKER.with(|force| force.replace(true)))
    }
}
impl Drop for ComponentWalkerGuard {
    fn drop(&mut self) {
        SCAN_FORCE_COMPONENT_WALKER.with(|force| force.set(self.0));
    }
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[test]
#[ignore = "explicit current edited-corpus scanner comparison; root serial slot required"]
fn frozen_kernel_relative_scanner_comparison() {
    const CORPUS: &str = "/private/tmp/openclank-s07-real-corpus.qvGHg5/codebase";
    const MANIFEST: &str = "/private/tmp/openclank-graph-source-s07.Qbmvee/source-manifest.json";
    const MANIFEST_SHA: &str = "26b2795f0a8a5c306cae3be2b38682c7dd2d5e8dd64a17170a8270bb5746f239";
    fn pin_file(path: &Path, sha: &str) {
        let metadata = std::fs::symlink_metadata(path).expect("calibration pin missing");
        assert!(
            metadata.is_file()
                && !metadata.file_type().is_symlink()
                && metadata.len() <= 8 * 1024 * 1024
        );
        let output = std::process::Command::new("/usr/bin/shasum")
            .args(["-a", "256"])
            .arg(path)
            .output()
            .expect("pin digest command unavailable");
        assert!(
            output.status.success() && output.stdout.starts_with(format!("{sha} ").as_bytes()),
            "calibration pin changed"
        );
    }
    let pins = || {
        assert_eq!(
            std::env::var("OPENCLANK_SCAN_KERNEL_CALIBRATION")
                .ok()
                .as_deref(),
            Some(MANIFEST_SHA),
            "explicit root admission required"
        );
        pin_file(Path::new(MANIFEST), MANIFEST_SHA);
        pin_file(
            &Path::new(CORPUS).join("src/openclank/code_resource_bridge.py"),
            "2031e4145b621e84df32ec265df584654a57c65049243eb0d3bb9fa68776d838",
        );
    };
    pins();
    let root = Path::new(CORPUS);
    assert_eq!(
        root.canonicalize().expect("current corpus unavailable"),
        root,
        "no alternate corpus or alias"
    );
    assert_ne!(
        unsafe { libc::geteuid() },
        0,
        "local permission qualification requires ordinary UID"
    );
    assert!(
        scan_kernel_relative_supported(),
        "unsupported gate cannot qualify fast path"
    );
    let mut version = [0u8; 64];
    let mut length = version.len();
    let status = unsafe {
        libc::sysctlbyname(
            c"kern.osproductversion".as_ptr(),
            version.as_mut_ptr().cast(),
            &mut length,
            std::ptr::null_mut(),
            0,
        )
    };
    assert!(scan_kernel_version_response(status, length, &version));
    let os_version = std::str::from_utf8(&version[..length - 1]).unwrap();
    let identity = directory_identity(root).expect("corpus identity unavailable");
    assert!(ScanRoot::new(root, &identity).unwrap().kernel_relative);
    let scan = |component: bool| {
        let _guard = component.then(ComponentWalkerGuard::force);
        assert_eq!(
            ScanRoot::new(root, &identity).unwrap().kernel_relative,
            !component
        );
        let start = std::time::Instant::now();
        let (plan, metrics) = crate::code_metrics::capture_code_metrics(|| {
            scan_codebase_pinned(root, &identity).expect("calibration scan refused")
        });
        let elapsed_ns = start.elapsed().as_nanos();
        let row = serde_json::json!({"strategy":if component {"scan_local_component"}else{"kernel_relative"}, "elapsed_ns":elapsed_ns,
            "files":plan.files.len(), "coverage":plan.coverage.len(), "policy_inputs":plan.policy.inputs.len(), "policy_bytes_read":plan.policy_bytes_read, "metrics":metrics});
        (plan, row)
    };
    let equal = |left: &ScanPlan, right: &ScanPlan| {
        assert!(left.files == right.files, "selected files mismatch");
        assert!(
            serde_json::to_value(&left.coverage).unwrap()
                == serde_json::to_value(&right.coverage).unwrap(),
            "full coverage mismatch"
        );
        assert!(
            left.policy.inputs == right.policy.inputs
                && left.policy.digest() == right.policy.digest(),
            "policy mismatch"
        );
        assert_eq!(left.policy_bytes_read, right.policy_bytes_read);
    };
    let (oracle, old_warm) = scan(true);
    let (warm, new_warm) = scan(false);
    equal(&oracle, &warm);
    println!(
        "SCAN_KERNEL_COMPARISON {}",
        serde_json::json!({"schema":1,"phase":"excluded_warmup","os_product_version":os_version,"capability":true,"samples":[old_warm,new_warm],"exact_full_output_parity":true})
    );
    drop(warm);
    for pair in 0..3 {
        let mut rows = Vec::new();
        for component in if pair % 2 == 0 {
            [true, false]
        } else {
            [false, true]
        } {
            let (plan, row) = scan(component);
            equal(&oracle, &plan);
            rows.push(row);
        }
        assert_eq!(
            directory_identity(root).expect("corpus identity lost"),
            identity
        );
        println!(
            "SCAN_KERNEL_COMPARISON {}",
            serde_json::json!({"schema":1,"phase":"measured_pair","pair":pair,"os_product_version":os_version,"capability":true,"samples":rows,"exact_full_output_parity":true})
        );
    }
    pins();
}

#[test]
fn kernel_version_gate_is_strict_bounded_and_fail_closed() {
    for value in [b"11.0.1\0".as_slice(), b"11.1\0", b"12.0\0", b"26.0.1\0"] {
        assert!(scan_kernel_version_supported(value));
    }
    for value in [
        b"11.0\0".as_slice(),
        b"10.15.7\0",
        b"11\0",
        b"\0",
        b"11.0.1",
        b"11..1\0",
        b"11.0.1.2\0",
        b"11.0.1beta\0",
        b" 11.0.1\0",
        b"+11.0.1\0",
        b"4294967296.0.0\0",
        b"11.0.1\0junk\0",
        b"\xff.0.1\0",
    ] {
        assert!(!scan_kernel_version_supported(value));
    }
    assert!(!scan_kernel_version_supported(&[b'1'; 65]));
    let mut buffer = [0u8; 64];
    buffer[..7].copy_from_slice(b"11.0.1\0");
    assert!(scan_kernel_version_response(0, 7, &buffer));
    for (result, length) in [(-1, 7), (0, 0), (0, 6), (0, 64), (0, 65), (0, usize::MAX)] {
        assert!(!scan_kernel_version_response(result, length, &buffer));
    }
}

#[test]
fn kernel_and_current_component_scans_have_exact_full_output_parity() {
    let f = Fixture::new();
    for (path, bytes) in [
        (".gitignore", "*.tmp\n!keep.tmp\n"),
        ("a/.gitignore", "skip.py\n"),
        ("a/b/c.py", "value = 1"),
        ("a/skip.py", "denied=1"),
        ("keep.tmp", "asset"),
        (".clanker/secret.py", "private"),
        ("empty.py", ""),
        ("media.png", "asset"),
    ] {
        f.write(path, bytes);
    }
    symlink(f.base.join("foreign"), f.root.join("link")).unwrap();
    let expected = directory_identity(&f.root).unwrap();
    let old = {
        let _guard = ComponentWalkerGuard::force();
        let pin = ScanRoot::new(&f.root, &expected).unwrap();
        assert!(!pin.kernel_relative);
        scan_codebase_pinned(&f.root, &expected).unwrap()
    };
    let new = scan_codebase_pinned(&f.root, &expected).unwrap();
    assert_eq!(old.files, new.files);
    assert_eq!(
        serde_json::to_value(old.coverage).unwrap(),
        serde_json::to_value(new.coverage).unwrap()
    );
    assert_eq!(old.policy.inputs, new.policy.inputs);
    assert_eq!(old.policy.digest(), new.policy.digest());
    assert_eq!(old.policy_bytes_read, new.policy_bytes_read);
}

#[test]
fn kernel_walk_structure_and_fresh_root_dot_are_explicit() {
    use std::io::Read;
    let f = Fixture::new();
    f.write("a/b/value.py", "value = 1");
    let expected = directory_identity(&f.root).unwrap();
    let old = {
        let _guard = ComponentWalkerGuard::force();
        ScanRoot::new(&f.root, &expected).unwrap()
    };
    old.open(&f.root.join("a/b/value.py")).unwrap();
    assert_eq!((old.opens.get(), old.intermediate_stats.get()), (4, 2));
    let new = ScanRoot::new(&f.root, &expected).unwrap();
    let mut text = String::new();
    new.open(&f.root.join("a/b/value.py"))
        .unwrap()
        .read_to_string(&mut text)
        .unwrap();
    assert_eq!(text, "value = 1");
    assert_eq!(
        (new.opens.get(), new.intermediate_stats.get()),
        if new.kernel_relative { (2, 0) } else { (4, 2) }
    );
    let before = new.opens.get();
    let first = pinned_directory_names(&new.open(&f.root).unwrap()).unwrap();
    let second = pinned_directory_names(&new.open(&f.root).unwrap()).unwrap();
    assert_eq!(first, second);
    assert_eq!(
        new.opens.get() - before,
        2,
        "fresh independent root dot each time"
    );
}

#[test]
fn kernel_walk_rejects_every_symlink_position_escape_and_non_directory() {
    let f = Fixture::new();
    f.write("a/b/value.py", "x=1");
    f.write("plain", "not a directory");
    symlink("a", f.root.join("early")).unwrap();
    symlink("b", f.root.join("a/middle")).unwrap();
    symlink("value.py", f.root.join("a/b/final.py")).unwrap();
    let expected = directory_identity(&f.root).unwrap();
    for force_component in [true, false] {
        let guard = force_component.then(ComponentWalkerGuard::force);
        let pin = ScanRoot::new(&f.root, &expected).unwrap();
        for path in [
            "early/b/value.py",
            "a/middle/value.py",
            "a/b/final.py",
            "plain/child",
            "a/../plain",
        ] {
            let before = pin.opens.get();
            assert!(pin.open(&f.root.join(path)).is_err(), "must reject {path}");
            if pin.kernel_relative && !path.contains("..") {
                assert_eq!(pin.opens.get() - before, 2, "no retry after kernel refusal");
            }
        }
        assert!(pin.open(&f.base.join("outside")).is_err());
        drop(guard);
    }
}

#[test]
fn kernel_relative_search_permission_is_not_parent_list_authority() {
    use std::os::unix::fs::PermissionsExt;
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("permission fixture unqualified: effective uid 0 bypasses DAC");
        return;
    }
    let f = Fixture::new();
    f.write("a/b/value.py", "x=1");
    let expected = directory_identity(&f.root).unwrap();
    let old = {
        let _guard = ComponentWalkerGuard::force();
        ScanRoot::new(&f.root, &expected).unwrap()
    };
    let new = ScanRoot::new(&f.root, &expected).unwrap();
    struct Restore(PathBuf);
    impl Drop for Restore {
        fn drop(&mut self) {
            std::fs::set_permissions(&self.0, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
    }
    let parent = f.root.join("a");
    let _restore = Restore(parent.clone());
    std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o111)).unwrap();
    assert!(old.open(&f.root.join("a/b/value.py")).is_err());
    assert_eq!(
        new.open(&f.root.join("a/b/value.py")).is_ok(),
        new.kernel_relative
    );
    // A scan still must enumerate every selected directory readably.
    assert!(scan_codebase_pinned(&f.root, &expected).is_err());
    std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o000)).unwrap();
    let before = new.opens.get();
    assert!(new.open(&f.root.join("a/b/value.py")).is_err());
    if new.kernel_relative {
        assert_eq!(new.opens.get() - before, 2);
    }
}

struct Fixture {
    base: PathBuf,
    root: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let base = std::env::temp_dir().join(format!("fm-scan-root-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&base).unwrap();
        let base = base.canonicalize().unwrap();
        let root = base.join("repo");
        std::fs::create_dir(&root).unwrap();
        Self { base, root }
    }
    fn write(&self, path: &str, bytes: &str) {
        let path = self.root.join(path);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, bytes).unwrap();
    }
    fn parity(&self) -> ScanPlan {
        let expected = directory_identity(&self.root).unwrap();
        ABSOLUTE_ROOT_SETUPS.with(|n| n.set(0));
        let old = scan_codebase_inventory_mode(&self.root, &expected, None, false).unwrap();
        let old_count = ABSOLUTE_ROOT_SETUPS.with(|n| n.get());
        ABSOLUTE_ROOT_SETUPS.with(|n| n.set(0));
        let new = scan_codebase_pinned(&self.root, &expected).unwrap();
        assert_eq!(
            ABSOLUTE_ROOT_SETUPS.with(|n| n.get()),
            1,
            "one absolute setup per scan"
        );
        assert!(old_count > 1);
        assert_eq!(old.files, new.files);
        assert_eq!(
            serde_json::to_value(old.coverage).unwrap(),
            serde_json::to_value(&new.coverage).unwrap()
        );
        assert_eq!(old.policy.inputs, new.policy.inputs);
        assert_eq!(old.policy.digest(), new.policy.digest());
        assert_eq!(old.policy_bytes_read, new.policy_bytes_read);
        new
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.base);
    }
}

#[test]
fn exact_inventory_controls_and_structural_setup_parity() {
    let f = Fixture::new();
    for (path, bytes) in [
        (".gitignore", "*.tmp\n!keep.tmp\n"),
        ("src/.gitignore", "skip.py\n"),
        ("src/a.py", "def alpha(): pass"),
        ("src/skip.py", "x=1"),
        ("keep.tmp", "asset"),
        ("gone.tmp", "asset"),
        (".clanker/private.py", "secret=1"),
        ("node_modules/n.py", "x=1"),
        ("data/image.png", "asset"),
        ("src/nested/b.rs", "fn beta() {}"),
    ] {
        f.write(path, bytes);
    }
    symlink(f.base.join("elsewhere"), f.root.join("link")).unwrap();
    let scan = f.parity();
    assert!(scan.files.contains(&f.root.join("src/a.py")));
    assert!(!scan.files.contains(&f.root.join("src/skip.py")));
    assert!(scan
        .coverage
        .iter()
        .any(|c| c.rel_path == "link" && c.status == "outside_root"));
    assert_eq!(
        scan.policy_bytes_read,
        2 * ("*.tmp\n!keep.tmp\n".len() + "skip.py\n".len())
    );
}

#[test]
fn fresh_nested_membership_and_same_stat_control_bytes() {
    let f = Fixture::new();
    f.write("a.py", "x=1");
    f.write(".gitignore", "a.py\n");
    let before = f.parity();
    let file = std::fs::OpenOptions::new()
        .write(true)
        .open(f.root.join(".gitignore"))
        .unwrap();
    let metadata = file.metadata().unwrap();
    f.write(".gitignore", "b.py\n");
    file.set_times(std::fs::FileTimes::new().set_modified(metadata.modified().unwrap()))
        .unwrap();
    let after = f.parity();
    assert_ne!(before.policy.digest(), after.policy.digest());
    assert!(after.files.contains(&f.root.join("a.py")));
    f.write("new/nested/.gitignore", "hidden.py\n");
    f.write("new/nested/hidden.py", "x=1");
    let nested = f.parity();
    assert_ne!(after.policy.digest(), nested.policy.digest());
    assert!(!nested.files.contains(&f.root.join("new/nested/hidden.py")));
    std::fs::remove_file(f.root.join("new/nested/.gitignore")).unwrap();
    assert!(f
        .parity()
        .files
        .contains(&f.root.join("new/nested/hidden.py")));
}

#[test]
fn root_and_ancestor_replacement_rejected_but_same_inode_alias_preserved() {
    let f = Fixture::new();
    f.write("src/a.py", "x=1");
    let expected = directory_identity(&f.root).unwrap();
    let pin = ScanRoot::new(&f.root, &expected).unwrap();
    let moved = f.base.join("moved");
    std::fs::rename(&f.root, &moved).unwrap();
    std::fs::create_dir(&f.root).unwrap();
    assert!(pin.open(&f.root.join("src/a.py")).is_err());
    std::fs::remove_dir(&f.root).unwrap();
    symlink(&moved, &f.root).unwrap();
    assert!(pin.open(&f.root.join("src/a.py")).is_ok());
    assert!(open_pinned_path(&f.root, &f.root.join("src/a.py"), &expected).is_ok());
    // Swap a path ancestor, so the old pin still exists but the declared path
    // now reaches an unrelated root.
    std::fs::remove_file(&f.root).unwrap();
    std::fs::rename(&moved, &f.root).unwrap();
    let ancestor_alias = f.base.join("alias");
    symlink(&f.base, &ancestor_alias).unwrap();
    let alias_root = ancestor_alias.join("repo");
    let alias_pin = ScanRoot::new(&alias_root, &expected).unwrap();
    std::fs::remove_file(&ancestor_alias).unwrap();
    std::fs::create_dir(&ancestor_alias).unwrap();
    std::fs::create_dir(ancestor_alias.join("repo")).unwrap();
    assert!(alias_pin.open(&alias_root).is_err());
}

#[test]
fn relative_symlink_rename_control_and_invalid_path_fail_closed() {
    let f = Fixture::new();
    f.write("src/.gitignore", "a.py\n");
    f.write("src/a.py", "x=1");
    let expected = directory_identity(&f.root).unwrap();
    let pin = ScanRoot::new(&f.root, &expected).unwrap();
    let policy = f.parity().policy;
    let outside = f.base.join("outside");
    std::fs::create_dir(&outside).unwrap();
    std::fs::rename(f.root.join("src"), f.root.join("old-src")).unwrap();
    symlink(&outside, f.root.join("src")).unwrap();
    assert!(pin.open(&f.root.join("src/a.py")).is_err());
    assert!(pin.validate_policy(&policy).is_err());
    assert!(pin.open(&f.root.join("../outside")).is_err());
    assert!(pin.open(&outside).is_err());
    std::fs::remove_file(f.root.join("src")).unwrap();
    std::fs::rename(f.root.join("old-src"), f.root.join("src")).unwrap();
    std::fs::remove_file(f.root.join("src/.gitignore")).unwrap();
    symlink(outside, f.root.join("src/.gitignore")).unwrap();
    assert!(pin.ignore_bytes(&f.root.join("src")).is_err());
}

#[test]
fn enumeration_offsets_are_independent_and_cancel_is_unchanged() {
    let f = Fixture::new();
    f.write("a.py", "x=1");
    let expected = directory_identity(&f.root).unwrap();
    let pin = ScanRoot::new(&f.root, &expected).unwrap();
    let first = pinned_directory_names(&pin.open(&f.root).unwrap()).unwrap();
    let second = pinned_directory_names(&pin.open(&f.root).unwrap()).unwrap();
    assert_eq!(first, second);
    assert_eq!(first.len(), 1);
    assert_eq!(pin.boundaries.get(), 2);
    let cancelled = std::sync::atomic::AtomicBool::new(true);
    assert!(
        scan_codebase_inventory_mode(&f.root, &expected, Some((&cancelled, 100)), true)
            .unwrap_err()
            .contains("cancelled")
    );
    assert!(
        scan_codebase_inventory_mode(&f.root, &expected, Some((&cancelled, 100)), false)
            .unwrap_err()
            .contains("cancelled")
    );
    assert!(scan_visual_inventory(
        &f.root,
        &expected,
        &std::sync::atomic::AtomicBool::new(false),
        0
    )
    .is_err());
}

#[test]
fn invalid_and_oversize_controls_reject_on_both_paths() {
    let f = Fixture::new();
    f.write(".gitignore", "[z-a]\n");
    let expected = directory_identity(&f.root).unwrap();
    for reuse in [false, true] {
        assert!(scan_codebase_inventory_mode(&f.root, &expected, None, reuse).is_err());
    }
    f.write(".gitignore", &"x".repeat(MAX_IGNORE_FILE_BYTES + 1));
    for reuse in [false, true] {
        assert!(scan_codebase_inventory_mode(&f.root, &expected, None, reuse).is_err());
    }
    std::fs::write(f.root.join(".gitignore"), [0xff, 0xfe]).unwrap();
    for reuse in [false, true] {
        assert!(
            scan_codebase_inventory_mode(&f.root, &expected, None, reuse)
                .unwrap_err()
                .contains("expected UTF-8")
        );
    }
}

#[test]
fn current_file_replacement_refuses_and_fresh_scan_recovers() {
    let f = Fixture::new();
    let expected = directory_identity(&f.root).unwrap();
    let pin = ScanRoot::new(&f.root, &expected).unwrap();
    std::fs::rename(&f.root, f.base.join("saved")).unwrap();
    std::fs::write(&f.root, "not a directory").unwrap();
    assert!(pin.open(&f.root).is_err());
    drop(pin);
    std::fs::remove_file(&f.root).unwrap();
    std::fs::rename(f.base.join("saved"), &f.root).unwrap();
    f.parity();
}

#[test]
fn searchable_ancestor_without_list_permission_and_fresh_permission_denials() {
    use std::os::unix::fs::PermissionsExt;
    // Root can bypass POSIX DAC, so this fixture cannot prove permission
    // refusal in that execution domain. No equivalent claim is made there.
    if unsafe { libc::geteuid() } == 0 {
        eprintln!("permission fixture not applicable: effective uid 0 bypasses DAC");
        return;
    }
    struct Restore(Vec<(PathBuf, std::fs::Permissions)>);
    impl Drop for Restore {
        fn drop(&mut self) {
            // Restore ancestor first, so descendant paths are searchable.
            for (path, permissions) in &self.0 {
                let _ = std::fs::set_permissions(path, permissions.clone());
            }
        }
    }
    let f = Fixture::new();
    f.write(".gitignore", "hidden.py\n");
    f.write("visible.py", "x=1");
    let control = f.root.join(".gitignore");
    let _restore = Restore(
        [&f.base, &f.root, &control]
            .into_iter()
            .map(|p| (p.clone(), std::fs::metadata(p).unwrap().permissions()))
            .collect(),
    );
    let expected = directory_identity(&f.root).unwrap();
    let pin = ScanRoot::new(&f.root, &expected).unwrap();
    std::fs::set_permissions(&f.base, std::fs::Permissions::from_mode(0o111)).unwrap();
    // Explicitly accepted difference: traversing an admitted readable root
    // needs ancestor search, not ancestor directory-list/read permission.
    assert!(open_pinned_path(&f.root, &f.root, &expected).is_err());
    assert!(pin.open(&f.root).is_ok());
    assert!(
        scan_codebase_pinned(&f.root, &expected).is_err(),
        "initial setup remains unchanged"
    );
    assert_eq!(pin.ignore_bytes(&f.root).unwrap().unwrap(), b"hidden.py\n");
    std::fs::set_permissions(&f.root, std::fs::Permissions::from_mode(0o000)).unwrap();
    assert!(pin.open(&f.root).is_err());
    std::fs::set_permissions(&f.root, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::set_permissions(&control, std::fs::Permissions::from_mode(0o000)).unwrap();
    assert!(pin.ignore_bytes(&f.root).is_err());
    std::fs::set_permissions(&control, std::fs::Permissions::from_mode(0o600)).unwrap();
    std::fs::set_permissions(&f.base, std::fs::Permissions::from_mode(0o000)).unwrap();
    assert!(
        pin.open(&f.root).is_err(),
        "ancestor search revocation still refuses"
    );
}
