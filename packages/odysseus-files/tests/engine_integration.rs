use odysseus_files::*;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use tempfile::tempdir;

fn registry_for(root: &Path) -> RootRegistry {
    let mut registry = RootRegistry::default();
    let canonical = fs::canonicalize(root).unwrap();
    registry
        .register(RootRecord {
            id: "root-1".into(),
            owner_id: "owner-1".into(),
            kind: RootKind::RecursiveDirectory,
            canonical_path: canonical.to_string_lossy().into_owned(),
            display_path: root.to_string_lossy().into_owned(),
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

fn scope() -> AgentScope {
    AgentScope::new(["root-1".to_string()])
}

fn register_root(
    registry: &mut RootRegistry,
    id: &str,
    owner_id: &str,
    root: &Path,
    capabilities: &[Capability],
) {
    let canonical = fs::canonicalize(root).unwrap();
    registry
        .register(RootRecord {
            id: id.into(),
            owner_id: owner_id.into(),
            kind: RootKind::RecursiveDirectory,
            canonical_path: canonical.to_string_lossy().into_owned(),
            display_path: canonical.to_string_lossy().into_owned(),
            enabled: true,
            capabilities: capabilities.iter().copied().collect(),
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
}

#[test]
fn directory_pages_are_metadata_only_and_cursor_bounded() {
    let directory = tempdir().unwrap();
    for index in 0..5 {
        fs::write(
            directory.path().join(format!("{index}.txt")),
            format!("body-{index}"),
        )
        .unwrap();
    }
    fs::create_dir(directory.path().join("nested")).unwrap();
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 2,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let first = engine
        .list_directory("owner-1", &scope(), directory.path(), None)
        .unwrap();
    assert_eq!(first.entries.len(), 2);
    assert!(first.next_cursor.is_some());
    assert!(first
        .entries
        .iter()
        .all(|entry| entry.size > 0 || entry.kind == FileKind::Directory));
    assert!(first.work.entries_visited <= 3);
    let second = engine
        .list_directory(
            "owner-1",
            &scope(),
            directory.path(),
            first.next_cursor.as_ref(),
        )
        .unwrap();
    assert_eq!(second.entries.len(), 2);
    assert!(second.next_cursor.is_some());
}

#[test]
fn directory_page_keeps_the_single_lookahead_entry() {
    let directory = tempdir().unwrap();
    for index in 0..3 {
        fs::write(
            directory.path().join(format!("{index}.txt")),
            format!("body-{index}"),
        )
        .unwrap();
    }
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 2,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let first = engine
        .list_directory("owner-1", &scope(), directory.path(), None)
        .unwrap();
    assert_eq!(first.entries.len(), 2);
    assert!(first.next_cursor.is_some());
    let second = engine
        .list_directory(
            "owner-1",
            &scope(),
            directory.path(),
            first.next_cursor.as_ref(),
        )
        .unwrap();
    assert_eq!(second.entries.len(), 1);
    assert!(second.next_cursor.is_none());
    let names = first
        .entries
        .into_iter()
        .chain(second.entries)
        .map(|entry| entry.name)
        .collect::<BTreeSet<_>>();
    assert_eq!(
        names,
        BTreeSet::from(["0.txt".into(), "1.txt".into(), "2.txt".into()])
    );
}

#[test]
fn directory_page_limit_can_change_across_cursor_continuations() {
    let directory = tempdir().unwrap();
    for index in 0..238 {
        fs::write(
            directory.path().join(format!("{index:03}.txt")),
            index.to_string(),
        )
        .unwrap();
    }
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 200,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );

    let first = engine
        .list_directory_with_limit("owner-1", &scope(), directory.path(), None, 1)
        .unwrap();
    assert_eq!(first.entries.len(), 1);
    let second = engine
        .list_directory_with_limit(
            "owner-1",
            &scope(),
            directory.path(),
            first.next_cursor.as_ref(),
            37,
        )
        .unwrap();
    assert_eq!(second.entries.len(), 37);
    let third = engine
        .list_directory_with_limit(
            "owner-1",
            &scope(),
            directory.path(),
            second.next_cursor.as_ref(),
            200,
        )
        .unwrap();
    assert_eq!(third.entries.len(), 200);
    assert!(third.next_cursor.is_none());

    let names = first
        .entries
        .into_iter()
        .chain(second.entries)
        .chain(third.entries)
        .map(|entry| entry.name)
        .collect::<BTreeSet<_>>();
    assert_eq!(names.len(), 238);
    for index in 0..238 {
        assert!(names.contains(&format!("{index:03}.txt")));
    }
}

#[test]
fn sorted_directory_page_limit_can_change_without_skips_or_duplicates() {
    let directory = tempdir().unwrap();
    for index in 0..238 {
        fs::write(
            directory.path().join(format!("{index:03}.txt")),
            index.to_string(),
        )
        .unwrap();
    }
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 200,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort::default();

    let first = engine
        .list_directory_sorted_with_limit("owner-1", &scope(), directory.path(), None, &sort, 1)
        .unwrap();
    let second = engine
        .list_directory_sorted_with_limit(
            "owner-1",
            &scope(),
            directory.path(),
            first.next_cursor.as_ref(),
            &sort,
            37,
        )
        .unwrap();
    let third = engine
        .list_directory_sorted_with_limit(
            "owner-1",
            &scope(),
            directory.path(),
            second.next_cursor.as_ref(),
            &sort,
            200,
        )
        .unwrap();

    assert_eq!(first.entries.len(), 1);
    assert_eq!(second.entries.len(), 37);
    assert_eq!(third.entries.len(), 200);
    assert!(third.next_cursor.is_none());
    let names = first
        .entries
        .into_iter()
        .chain(second.entries)
        .chain(third.entries)
        .map(|entry| entry.name)
        .collect::<Vec<_>>();
    assert_eq!(names.len(), 238);
    assert_eq!(names.first().map(String::as_str), Some("000.txt"));
    assert_eq!(names.last().map(String::as_str), Some("237.txt"));
    assert_eq!(names.iter().collect::<BTreeSet<_>>().len(), 238);
}

#[test]
fn directory_page_limit_rejects_zero_and_values_above_the_configured_maximum() {
    let directory = tempdir().unwrap();
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 200,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort::default();

    assert!(matches!(
        engine.list_directory_with_limit("owner-1", &scope(), directory.path(), None, 0),
        Err(EngineError::LimitExceeded)
    ));
    assert!(matches!(
        engine.list_directory_sorted_with_limit(
            "owner-1",
            &scope(),
            directory.path(),
            None,
            &sort,
            201,
        ),
        Err(EngineError::LimitExceeded)
    ));
}

#[test]
fn sorted_directory_pages_bind_direction_and_keep_folders_first() {
    let directory = tempdir().unwrap();
    fs::create_dir(directory.path().join("z-folder")).unwrap();
    fs::write(directory.path().join("a.txt"), b"a").unwrap();
    fs::write(directory.path().join("m.txt"), b"m").unwrap();
    fs::write(directory.path().join("z.txt"), b"z").unwrap();
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 2,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort {
        key: "name".into(),
        direction: "desc".into(),
        directories_first: true,
        collation: "open-clank-v1".into(),
    };
    let first = engine
        .list_directory_sorted("owner-1", &scope(), directory.path(), None, &sort)
        .unwrap();
    let cursor = first.next_cursor.clone();
    let second = engine
        .list_directory_sorted(
            "owner-1",
            &scope(),
            directory.path(),
            cursor.as_ref(),
            &sort,
        )
        .unwrap();
    let names = first
        .entries
        .into_iter()
        .chain(second.entries)
        .map(|entry| entry.name)
        .collect::<Vec<_>>();
    assert_eq!(names, vec!["z-folder", "z.txt", "m.txt", "a.txt"]);
    let ascending = DirectorySort {
        direction: "asc".into(),
        ..sort
    };
    assert!(matches!(
        engine.list_directory_sorted(
            "owner-1",
            &scope(),
            directory.path(),
            cursor.as_ref(),
            &ascending
        ),
        Err(EngineError::StaleCursor)
    ));
}

#[test]
fn sorted_directory_pages_reuse_a_bounded_metadata_snapshot() {
    let directory = tempdir().unwrap();
    for index in 0..5 {
        fs::write(
            directory.path().join(format!("{index}.txt")),
            format!("body-{index}"),
        )
        .unwrap();
    }
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 2,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort::default();
    let first = engine
        .list_directory_sorted("owner-1", &scope(), directory.path(), None, &sort)
        .unwrap();
    assert_eq!(first.work.entries_visited, 5);
    let second = engine
        .list_directory_sorted(
            "owner-1",
            &scope(),
            directory.path(),
            first.next_cursor.as_ref(),
            &sort,
        )
        .unwrap();
    assert_eq!(second.work.entries_visited, 0);
    assert_eq!(second.entries.len(), 2);
}

#[test]
fn fresh_sorted_listing_observes_child_metadata_without_directory_mutation() {
    let directory = tempdir().unwrap();
    fs::write(directory.path().join("small.txt"), b"a").unwrap();
    fs::write(directory.path().join("medium.txt"), b"bb").unwrap();
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 10,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort {
        key: "size".into(),
        direction: "asc".into(),
        directories_first: true,
        collation: "open-clank-v1".into(),
    };

    let first = engine
        .list_directory_sorted("owner-1", &scope(), directory.path(), None, &sort)
        .unwrap();
    assert_eq!(
        first
            .entries
            .iter()
            .map(|entry| entry.name.as_str())
            .collect::<Vec<_>>(),
        vec!["small.txt", "medium.txt"],
    );

    // Updating a child's bytes does not normally update the parent directory
    // inode metadata. A new first-page request must still rebuild metadata and
    // sort order instead of reusing the previous presentation snapshot.
    fs::write(directory.path().join("small.txt"), b"now-larger").unwrap();
    let refreshed = engine
        .list_directory_sorted("owner-1", &scope(), directory.path(), None, &sort)
        .unwrap();
    assert_eq!(refreshed.work.entries_visited, 2);
    assert_eq!(
        refreshed
            .entries
            .iter()
            .map(|entry| (entry.name.as_str(), entry.size))
            .collect::<Vec<_>>(),
        vec![("medium.txt", 2), ("small.txt", 10)],
    );
}

#[test]
fn sorted_directory_cursor_detects_changes_inside_the_same_millisecond() {
    let directory = tempdir().unwrap();
    fs::write(directory.path().join("a.txt"), b"a").unwrap();
    fs::write(directory.path().join("b.txt"), b"b").unwrap();
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 1,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort::default();
    let first = engine
        .list_directory_sorted("owner-1", &scope(), directory.path(), None, &sort)
        .unwrap();
    let cursor = first.next_cursor.clone().expect("second page");

    fs::write(directory.path().join("c.txt"), b"c").unwrap();

    assert!(matches!(
        engine.list_directory_sorted("owner-1", &scope(), directory.path(), Some(&cursor), &sort,),
        Err(EngineError::StaleCursor)
    ));
}

#[test]
fn sorted_directory_page_keeps_the_remainder_after_a_full_page() {
    let directory = tempdir().unwrap();
    for index in 0..201 {
        fs::write(directory.path().join(format!("{index:03}.txt")), b"x").unwrap();
    }
    let engine = FileEngine::new(
        EngineConfig {
            directory_page_size: 200,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let sort = DirectorySort::default();
    let first = engine
        .list_directory_sorted("owner-1", &scope(), directory.path(), None, &sort)
        .unwrap();
    assert_eq!(first.entries.len(), 200);
    let second = engine
        .list_directory_sorted(
            "owner-1",
            &scope(),
            directory.path(),
            first.next_cursor.as_ref(),
            &sort,
        )
        .unwrap();
    assert_eq!(second.entries.len(), 1);
    assert!(second.next_cursor.is_none());
}

#[test]
fn range_reads_are_bounded_and_fingerprints_are_on_demand() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("note.md");
    fs::write(&file, b"0123456789").unwrap();
    let engine = FileEngine::new(
        EngineConfig {
            max_read_bytes: 4,
            ..EngineConfig::default()
        },
        registry_for(directory.path()),
    );
    let page = engine
        .read_range("owner-1", &scope(), &file, 2, 4, false)
        .unwrap();
    assert_eq!(page.bytes, b"2345");
    assert_eq!(page.next_offset, Some(6));
    assert_eq!(page.work.bytes_read, 4);
    assert!(page.fingerprint.is_none());
    let hashed = engine
        .read_range("owner-1", &scope(), &file, 0, 4, true)
        .unwrap();
    assert_eq!(hashed.fingerprint.as_ref().unwrap().algorithm, "sha256");
    assert_eq!(hashed.work.hashes_computed, 1);
    assert_eq!(
        engine
            .read_range("owner-1", &scope(), &file, 0, 5, false)
            .unwrap_err()
            .to_string(),
        "request exceeds the configured byte limit"
    );
}

#[test]
fn large_text_preview_reads_only_a_bounded_head_and_tail() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("large.log");
    let mut body = b"START\r\n".to_vec();
    body.extend(std::iter::repeat(b'x').take(2 * 1024 * 1024));
    body.extend_from_slice(b"\r\nTHE-END");
    fs::write(&file, &body).unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));

    let preview = engine
        .read_text_preview("owner-1", &scope(), &file, 320_000)
        .unwrap();

    assert!(preview.truncated);
    assert_eq!(preview.size, body.len() as u64);
    assert_eq!(preview.encoding, TextEncoding::Utf8);
    assert_eq!(preview.newline, "\r\n");
    assert!(preview.text.starts_with("START\r\n"));
    assert!(preview.text.ends_with("\r\nTHE-END"));
    assert!(preview.text.contains("preview truncated"));
    assert_eq!(preview.work.bytes_read, 320_000);
    assert_eq!(preview.work.hashes_computed, 0);
    assert!(matches!(
        engine.read_text_snapshot("owner-1", &scope(), &file),
        Err(EngineError::LimitExceeded)
    ));
}

#[test]
fn text_preview_preserves_utf16_bom_and_codepoint_boundaries() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("large-utf16.txt");
    let source = format!("BEGIN\r\n{}\r\nTAIL-🐙", "z".repeat(700_000));
    let mut bytes = vec![0xff, 0xfe];
    for word in source.encode_utf16() {
        bytes.extend_from_slice(&word.to_le_bytes());
    }
    fs::write(&file, &bytes).unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));

    let preview = engine
        .read_text_preview("owner-1", &scope(), &file, 320_001)
        .unwrap();

    assert!(preview.truncated);
    assert_eq!(preview.encoding, TextEncoding::Utf16Le);
    assert_eq!(preview.bom_bytes, 2);
    assert_eq!(preview.newline, "\r\n");
    assert!(preview.text.starts_with("BEGIN\r\n"));
    assert!(preview.text.ends_with("\r\nTAIL-🐙"));
    assert!(preview.work.bytes_read <= 320_001);
}

#[test]
fn text_preview_rejects_sampled_binary_without_loading_the_file() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("large.bin");
    let mut body = vec![b'a'; 2 * 1024 * 1024];
    body[3] = 1;
    fs::write(&file, body).unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));

    assert!(matches!(
        engine.read_text_preview("owner-1", &scope(), &file, 320_000),
        Err(EngineError::BinaryOrUnsupportedText)
    ));
}

#[test]
fn cas_replace_is_atomic_and_conflicts_without_publishing() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("note.md");
    fs::write(&file, b"old").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    let current = engine
        .read_range("owner-1", &scope(), &file, 0, 3, true)
        .unwrap()
        .fingerprint
        .unwrap();
    let replaced = engine
        .replace_if_fingerprint("owner-1", &scope(), &file, Some(&current), b"new")
        .unwrap();
    assert!(matches!(replaced, ReplaceOutcome::Replaced { .. }));
    assert_eq!(fs::read(&file).unwrap(), b"new");
    let wrong = Fingerprint {
        algorithm: "sha256",
        value: "sha256:wrong".into(),
    };
    assert_eq!(
        engine
            .replace_if_fingerprint("owner-1", &scope(), &file, Some(&wrong), b"bad")
            .unwrap_err()
            .to_string(),
        "expected fingerprint does not match the current file"
    );
    assert_eq!(fs::read(&file).unwrap(), b"new");
}

#[test]
fn denied_owner_and_path_never_reach_filesystem_work() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("secret.txt");
    fs::write(&file, b"secret").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    let denied = engine
        .read_range("owner-2", &scope(), &file, 0, 6, false)
        .unwrap_err();
    assert!(matches!(
        denied,
        EngineError::Registry(RegistryError::OutsideRoot)
    ));
    let sibling = directory.path().join("../secret.txt");
    let requested = BTreeSet::from([Capability::Read]);
    assert!(engine
        .registry()
        .authorize("owner-1", &sibling, &requested, &scope())
        .is_err());
}

#[test]
fn text_snapshots_preserve_bom_encoding_newline_and_mode_through_edit() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("note.txt");
    let mut bytes = vec![0xff, 0xfe];
    for word in "one\r\ntwo\r\n".encode_utf16() {
        bytes.extend_from_slice(&word.to_le_bytes());
    }
    fs::write(&file, bytes).unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    let snapshot = engine
        .read_text_snapshot("owner-1", &scope(), &file)
        .unwrap();
    assert_eq!(snapshot.encoding, TextEncoding::Utf16Le);
    assert_eq!(snapshot.bom_bytes, 2);
    assert_eq!(snapshot.newline, "\r\n");
    assert_eq!(snapshot.text, "one\r\ntwo\r\n");
    let edit = engine
        .edit_text(
            "owner-1",
            &scope(),
            &file,
            Some(&snapshot.fingerprint),
            "two",
            "three",
            false,
        )
        .unwrap();
    assert_eq!(edit.replacements, 1);
    assert_eq!(edit.encoding, TextEncoding::Utf16Le);
    assert_eq!(edit.newline, "\r\n");
    let after = engine
        .read_text_snapshot("owner-1", &scope(), &file)
        .unwrap();
    assert_eq!(after.text, "one\r\nthree\r\n");
    assert_eq!(after.bom_bytes, 2);
}

#[test]
fn binary_and_non_unique_text_edits_are_refused() {
    let directory = tempdir().unwrap();
    let binary = directory.path().join("data.bin");
    fs::write(&binary, [0, 1, 2, 3]).unwrap();
    let text = directory.path().join("note.txt");
    fs::write(&text, b"same\nsame\n").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    assert!(matches!(
        engine.read_text_snapshot("owner-1", &scope(), &binary),
        Err(EngineError::BinaryOrUnsupportedText)
    ));
    assert!(matches!(
        engine.edit_text("owner-1", &scope(), &text, None, "same", "new", false),
        Err(EngineError::Conflict)
    ));
}

#[test]
fn copy_move_and_recoverable_trash_authorize_both_paths() {
    let directory = tempdir().unwrap();
    let source = directory.path().join("source.txt");
    let copy = directory.path().join("copy.txt");
    let moved = directory.path().join("moved.txt");
    fs::write(&source, b"payload").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    let copied = engine
        .copy_file("owner-1", &scope(), &source, &copy)
        .unwrap();
    assert_eq!(copied.fingerprint.algorithm, "sha256");
    assert_eq!(fs::read(&copy).unwrap(), b"payload");
    engine
        .move_file("owner-1", &scope(), &copy, &moved)
        .unwrap();
    assert!(!copy.exists());
    assert!(moved.exists());
    let trash = engine.trash_file("owner-1", &scope(), &moved).unwrap();
    assert!(!moved.exists());
    assert!(trash.trashed_path.exists());
    engine.restore_file("owner-1", &scope(), &trash).unwrap();
    assert_eq!(fs::read(&moved).unwrap(), b"payload");
}

#[test]
fn approved_mutations_reject_a_source_changed_after_fingerprinting() {
    let directory = tempdir().unwrap();
    let source = directory.path().join("source.txt");
    let moved = directory.path().join("moved.txt");
    fs::write(&source, b"approved bytes").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    let approved = engine
        .stat("owner-1", &scope(), &source, true)
        .unwrap()
        .fingerprint
        .unwrap();

    fs::write(&source, b"changed after approval").unwrap();
    assert!(matches!(
        engine.move_file_if_fingerprint("owner-1", &scope(), &source, &moved, Some(&approved),),
        Err(EngineError::Conflict)
    ));
    assert!(source.exists());
    assert!(!moved.exists());

    let current = engine
        .stat("owner-1", &scope(), &source, true)
        .unwrap()
        .fingerprint
        .unwrap();
    let trash = engine
        .trash_file_if_fingerprint("owner-1", &scope(), &source, Some(&current))
        .unwrap();
    assert_eq!(trash.fingerprint.as_deref(), Some(current.value.as_str()));
    fs::write(&trash.trashed_path, b"changed in trash").unwrap();
    assert!(matches!(
        engine.restore_file_if_fingerprint("owner-1", &scope(), &trash, Some(&current)),
        Err(EngineError::Conflict)
    ));
    assert!(!source.exists());
    assert!(trash.trashed_path.exists());
}

#[test]
fn move_requires_write_authority_on_read_only_source_but_copy_does_not() {
    let directory = tempdir().unwrap();
    let read_root = directory.path().join("read-root");
    let write_root = directory.path().join("write-root");
    fs::create_dir(&read_root).unwrap();
    fs::create_dir(&write_root).unwrap();
    let source = read_root.join("source.txt");
    let copied = write_root.join("copied.txt");
    let moved = write_root.join("moved.txt");
    fs::write(&source, b"read-only source").unwrap();

    let mut registry = RootRegistry::default();
    register_root(
        &mut registry,
        "root-read",
        "owner-1",
        &read_root,
        &[Capability::Read],
    );
    register_root(
        &mut registry,
        "root-write",
        "owner-1",
        &write_root,
        &[Capability::Write],
    );
    let agent_scope = AgentScope::new(["root-read".to_string(), "root-write".to_string()]);
    let engine = FileEngine::new(EngineConfig::default(), registry);

    let copied_result = engine
        .copy_file("owner-1", &agent_scope, &source, &copied)
        .unwrap();
    assert_eq!(
        copied_result.destination,
        fs::canonicalize(&copied).unwrap()
    );
    let denied = engine
        .move_file("owner-1", &agent_scope, &source, &moved)
        .unwrap_err();
    assert!(matches!(
        denied,
        EngineError::Registry(RegistryError::OutsideRoot | RegistryError::CapabilityDenied)
    ));
    assert!(source.exists());
    assert!(!moved.exists());
}

#[test]
fn searches_are_lazy_bounded_and_cancellable() {
    let directory = tempdir().unwrap();
    fs::create_dir(directory.path().join("src")).unwrap();
    fs::create_dir(directory.path().join(".hidden")).unwrap();
    fs::write(directory.path().join("src/main.rs"), b"fn target() {}\n").unwrap();
    fs::write(directory.path().join(".hidden/secret.rs"), b"target\n").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(directory.path()));
    let options = SearchOptions {
        query: "main".into(),
        max_results: 10,
        max_entries: 10,
        max_depth: 4,
        max_bytes_per_file: 1024,
        include_hidden: false,
        case_sensitive: false,
    };
    let names = engine
        .filename_search("owner-1", &scope(), directory.path(), &options, None)
        .unwrap();
    assert_eq!(names.matches, vec![directory.path().join("src/main.rs")]);
    assert!(names.complete);

    let content_options = SearchOptions {
        query: "TARGET".into(),
        ..options
    };
    let content = engine
        .content_search(
            "owner-1",
            &scope(),
            directory.path(),
            &content_options,
            None,
        )
        .unwrap();
    assert_eq!(content.matches, vec![directory.path().join("src/main.rs")]);
    assert_eq!(content.work.bytes_read, 15);

    let cancelled = AtomicBool::new(true);
    assert!(matches!(
        engine.filename_search(
            "owner-1",
            &scope(),
            directory.path(),
            &content_options,
            Some(&cancelled)
        ),
        Err(EngineError::Cancelled)
    ));
}

#[cfg(unix)]
#[test]
fn symlink_escape_is_rejected_after_native_canonicalization() {
    use std::os::unix::fs::symlink;
    let root = tempdir().unwrap();
    let outside = tempdir().unwrap();
    let outside_file = outside.path().join("outside.txt");
    fs::write(&outside_file, b"secret").unwrap();
    let link = root.path().join("link.txt");
    symlink(&outside_file, &link).unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(root.path()));
    assert!(matches!(
        engine.read_range("owner-1", &scope(), &link, 0, 6, false),
        Err(EngineError::Registry(RegistryError::OutsideRoot))
    ));
}

#[test]
fn app_principal_reads_outside_agent_roots_without_expanding_scope() {
    let approved = tempdir().unwrap();
    let outside = tempdir().unwrap();
    let outside_file = outside.path().join("app-visible.txt");
    fs::write(&outside_file, b"app-visible").unwrap();
    let engine = FileEngine::new(EngineConfig::default(), registry_for(approved.path()));
    let page = engine.read_range_app(&outside_file, 0, 32, false).unwrap();
    assert_eq!(page.bytes, b"app-visible");
    assert!(engine
        .read_range("owner-1", &scope(), &outside_file, 0, 32, false)
        .is_err());
}
