//! Byte-equivalent cold metadata audit; no source/index/runtime fixture.
use super::*;
use rusqlite::types::Value;

const OLD_AUDIT: &str = "SELECT count(*) FROM files WHERE length(CAST(header AS BLOB))>65536 OR length(CAST(path AS BLOB))>4096 OR length(CAST(hash AS BLOB))!=64 OR length(CAST(payload_hash AS BLOB))!=64 OR length(CAST(resolution_hash AS BLOB))!=64 OR length(payload)>8388608 OR length(interface)>8388608 OR length(resolutions)>8388608";

fn new_audit() -> String {
    let mut sql = OLD_AUDIT.to_owned();
    for field in ["header", "path", "hash", "payload_hash", "resolution_hash"] {
        sql = sql.replace(
            &format!("length(CAST({field} AS BLOB))"),
            &format!("octet_length({field})"),
        );
    }
    assert!(include_str!("code_partitioned.rs").contains(sql.as_str()));
    sql
}

fn encoded_db(encoding: &str) -> Connection {
    assert!(["UTF-8", "UTF-16le", "UTF-16be"].contains(&encoding));
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch(&format!(
        "PRAGMA encoding='{encoding}'; CREATE TABLE values_under_test(value);"
    ))
    .unwrap();
    assert_eq!(
        db.query_row("PRAGMA encoding", [], |r| r.get::<_, String>(0))
            .unwrap(),
        encoding
    );
    db
}

#[test]
fn audit_octet_direct_column_preserves_encoded_bytes_for_all_storage_classes() {
    assert_eq!(
        rusqlite::version(),
        "3.49.1",
        "Qualify the actual bundled SQLite, not Python/system SQLite"
    );
    for encoding in ["UTF-8", "UTF-16le", "UTF-16be"] {
        let db = encoded_db(encoding);
        let mut values = vec![
            Value::Null,
            Value::Integer(0),
            Value::Integer(i64::MIN),
            Value::Real(1.25),
            Value::Real(-1.25e200),
            Value::Blob(vec![]),
            Value::Blob(vec![0, 255, 128, 65, 0]),
            Value::Text(String::new()),
            Value::Text("a\0b".into()),
            Value::Text("é🦀\0tail".into()),
        ];
        for length in [63, 64, 65, 4095, 4096, 4097, 65535, 65536, 65537] {
            values.push(Value::Text("x".repeat(length)));
            values.push(Value::Blob(vec![b'x'; length]));
        }
        for value in values {
            db.execute("DELETE FROM values_under_test", []).unwrap();
            db.execute("INSERT INTO values_under_test VALUES(?1)", params![value])
                .unwrap();
            let (old, new): (Option<i64>, Option<i64>) = db
                .query_row(
                    "SELECT length(CAST(value AS BLOB)),octet_length(value) FROM values_under_test",
                    [],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .unwrap();
            assert_eq!(
                old,
                new,
                "{encoding}, storage class {:?}",
                value.data_type()
            );
            if let Value::Text(text) = &value {
                let bytes = if encoding == "UTF-8" {
                    text.len()
                } else {
                    text.encode_utf16().count() * 2
                };
                assert_eq!(new, Some(bytes as i64));
            }
        }
    }
}

struct Fixture {
    root: PathBuf,
    path: PathBuf,
}

impl Fixture {
    fn new(encoding: &str, rows: usize, blob_bytes: usize) -> Self {
        let root = std::env::temp_dir().join(format!("fm-audit-octet-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("audit.sqlite");
        let db = Connection::open(&path).unwrap();
        db.execute_batch(&format!("PRAGMA encoding='{encoding}'; {SCHEMA}"))
            .unwrap();
        for key in ["files", "nodes", "edges"] {
            db.execute(
                "INSERT INTO meta VALUES(?1,?2)",
                params![
                    key,
                    if key == "files" {
                        rows.to_string()
                    } else {
                        "0".into()
                    }
                ],
            )
            .unwrap();
        }
        for id in 0..rows {
            db.execute("INSERT INTO files(id,path,hash,source_bytes,mtime,symbols,header,interface,payload,payload_hash,graph,resolutions,resolution_hash,node_count) VALUES(?1,?2,?3,0,0,0,'{}',zeroblob(?4),zeroblob(?4),?3,zeroblob(?4),zeroblob(?4),?3,0)",
                params![id as i64 + 1, format!("file{id}.py"), "a".repeat(if encoding == "UTF-8" { 64 } else { 32 }), blob_bytes as i64]).unwrap();
        }
        db.close().unwrap();
        Self { root, path }
    }

    fn reader(&self) -> Generation {
        let db =
            Connection::open_with_flags(&self.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                .unwrap();
        db.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-64; PRAGMA mmap_size=0;")
            .unwrap();
        // Only exercise audit_fields on an owned synthetic DB. These pins are
        // not authority, publication or a substitute for Generation::open.
        Generation {
            db: OwnedConnection(Some(db)),
            path: self.path.clone(),
            directory: CacheDirectory {
                path: self.root.clone(),
                pin: File::open(&self.root).unwrap(),
                guards: vec![],
            },
            pin: File::open(&self.path).unwrap(),
            remove: false,
            source_bytes: 0,
            records: 0,
            resolution_rows: 0,
            audit_memo: None,
        }
    }

    fn change(&self, field: &str, value: &Value) {
        assert!([
            "header",
            "path",
            "hash",
            "payload_hash",
            "resolution_hash",
            "payload",
            "interface",
            "resolutions"
        ]
        .contains(&field));
        let db = Connection::open(&self.path).unwrap();
        db.execute(
            &format!("UPDATE files SET {field}=?1 WHERE id=1"),
            params![value],
        )
        .unwrap();
        db.close().unwrap();
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        // Exact private UUID fixture only; all reader handles are scoped/dropped.
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}

#[test]
fn audit_octet_production_predicates_preserve_boundaries_and_count_failures() {
    for encoding in ["UTF-8", "UTF-16le", "UTF-16be"] {
        let fixture = Fixture::new(encoding, 1, 1);
        assert!(fixture.reader().audit_fields().is_ok());
        for (field, limit, exact) in [
            ("header", 65536, false),
            ("path", 4096, false),
            ("hash", 64, true),
            ("payload_hash", 64, true),
            ("resolution_hash", 64, true),
        ] {
            for delta in [-1_i64, 0, 1] {
                // BLOB storage preserves exact byte boundaries even in UTF16;
                // TEXT encoded-byte equivalence is independently checked above.
                let value = Value::Blob(vec![b'a'; (limit as i64 + delta) as usize]);
                fixture.change(field, &value);
                let generation = fixture.reader();
                let old: i64 = generation
                    .db
                    .query_row(OLD_AUDIT, [], |r| r.get(0))
                    .unwrap();
                assert_eq!(old != 0, if exact { delta != 0 } else { delta > 0 });
                assert_eq!(
                    generation.audit_fields().is_err(),
                    old != 0,
                    "{encoding}/{field}/{delta}"
                );
                drop(generation);
            }
            fixture.change(
                field,
                &Value::Text(if field == "header" {
                    "{}".into()
                } else if field == "path" {
                    "file0.py".into()
                } else {
                    "a".repeat(if encoding == "UTF-8" { 64 } else { 32 })
                }),
            );
        }
        // Nullable legacy field keeps SQL NULL behavior, not a new rejection.
        fixture.change("resolution_hash", &Value::Null);
        assert!(fixture.reader().audit_fields().is_ok());
        let db = Connection::open(&fixture.path).unwrap();
        db.execute("UPDATE meta SET value='999' WHERE key='nodes'", [])
            .unwrap();
        db.close().unwrap();
        assert!(fixture
            .reader()
            .audit_fields()
            .unwrap_err()
            .contains("incomplete partition index"));
    }
    for field in ["payload", "interface", "resolutions"] {
        let fixture = Fixture::new("UTF-8", 1, 1);
        fixture.change(field, &Value::Blob(vec![0; 8388609]));
        let generation = fixture.reader();
        assert_eq!(
            generation
                .db
                .query_row(OLD_AUDIT, [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert!(
            generation.audit_fields().is_err(),
            "Unchanged BLOB cap: {field}"
        );
    }
}

fn cache_misses(db: &Connection, reset: bool) -> i32 {
    let (mut current, mut highwater) = (0, 0);
    // Local bundled SQLite numeric page-cache observations, not OS disk I/O.
    let result = unsafe {
        rusqlite::ffi::sqlite3_db_status(
            db.handle(),
            rusqlite::ffi::SQLITE_DBSTATUS_CACHE_MISS,
            &mut current,
            &mut highwater,
            i32::from(reset),
        )
    };
    assert_eq!(result, rusqlite::ffi::SQLITE_OK);
    current
}

#[test]
fn audit_octet_avoids_overflow_page_loading_on_fresh_connections() {
    let fixture = Fixture::new("UTF-8", 4, 512 * 1024); // About8MiB, never the real corpus.
    let old = fixture.reader();
    cache_misses(&old.db, true);
    assert_eq!(
        old.db
            .query_row(OLD_AUDIT, [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    let old_misses = cache_misses(&old.db, false);
    drop(old);
    let new = fixture.reader();
    cache_misses(&new.db, true);
    new.audit_fields().unwrap(); // Actual production method, including counts.
    let new_misses = cache_misses(&new.db, false);
    assert!(
        old_misses >= 256 && new_misses * 8 < old_misses,
        "Cold SQLite page-cache misses: old={old_misses}, new={new_misses}"
    );
    for (sql, wanted) in [(OLD_AUDIT.to_owned(), 0_i64), (new_audit(), 192_i64)] {
        let mut stmt = new.db.prepare(&format!("EXPLAIN {sql}")).unwrap();
        let columns = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(6)?,
                ))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap()
            .into_iter()
            .filter(|r| r.0 == "Column")
            .collect::<Vec<_>>();
        for field in [6, 1, 2, 9, 12] {
            assert!(columns.iter().any(|r| r.1 == field && r.2 == wanted));
        }
        for field in [8, 7, 11] {
            assert!(columns.iter().any(|r| r.1 == field && r.2 == 64));
        }
    }
    eprintln!("S07_AUDIT_OCTET sqlite={} old_page_cache_misses={old_misses} new_page_cache_misses={new_misses} fixture_rows=4 blob_bytes=524288", rusqlite::version());
}
