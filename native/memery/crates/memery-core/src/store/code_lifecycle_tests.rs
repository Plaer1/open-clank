use super::*;
use serde_json::{json, Value};

fn fixture() -> (Connection, GraphScope) {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch("CREATE TABLE code_files(codebase TEXT NOT NULL,rel_path TEXT NOT NULL,blake3 TEXT NOT NULL,source_generation TEXT NOT NULL,PRIMARY KEY(codebase,rel_path));").unwrap();
    let scope = GraphScope {
        owner: "lifecycle-owner".into(),
        workspace_id: "lifecycle-workspace".into(),
        include_global: false,
    };
    (conn, scope)
}

fn insert(conn: &Connection, scope: &GraphScope, path: &str, hash: &str, generation: &str) {
    conn.execute(
        "INSERT INTO code_files VALUES(?1,?2,?3,?4)",
        params![
            SqliteStore::codebase_key(scope, "checkout"),
            path,
            hash,
            generation
        ],
    )
    .unwrap();
}

fn old(conn: &Connection, scope: &GraphScope, value: &mut Value) {
    if let Some(entries) = value.as_array_mut() {
        for entry in entries {
            if let Some(path) = entry["rel_path"].as_str() {
                let receipt: Option<(String, String)> = conn.query_row("SELECT blake3,source_generation FROM code_files WHERE codebase=?1 AND rel_path=?2",params![SqliteStore::codebase_key(scope,"checkout"),path],|r|Ok((r.get(0)?,r.get(1)?))).optional().unwrap();
                if let Some((hash, generation)) = receipt {
                    entry["source_hash"] = json!(hash);
                    entry["source_generation"] = json!(generation);
                }
            }
        }
    }
}

fn statements(conn: &Connection, work: impl FnOnce()) -> usize {
    unsafe extern "C" fn trace(
        _: u32,
        context: *mut std::ffi::c_void,
        _: *mut std::ffi::c_void,
        _: *mut std::ffi::c_void,
    ) -> i32 {
        unsafe {
            *(context as *mut usize) += 1;
        }
        0
    }
    struct Reset<'a>(&'a Connection);
    impl Drop for Reset<'_> {
        fn drop(&mut self) {
            unsafe {
                rusqlite::ffi::sqlite3_trace_v2(self.0.handle(), 0, None, std::ptr::null_mut());
            }
        }
    }
    let mut count = Box::new(0usize);
    unsafe {
        assert_eq!(
            rusqlite::ffi::sqlite3_trace_v2(
                conn.handle(),
                rusqlite::ffi::SQLITE_TRACE_STMT as u32,
                Some(trace),
                (&mut *count as *mut usize).cast()
            ),
            0
        );
    }
    let reset = Reset(conn);
    work();
    drop(reset);
    *count
}

#[test]
fn lifecycle_batch_overlay_matches_old_order_duplicates_types_and_scope() {
    let (conn, scope) = fixture();
    insert(
        &conn,
        &scope,
        "same.py",
        "new-hash",
        "old-original-generation",
    );
    insert(&conn, &scope, "nul\0é.py", "nul-hash", "nul-generation");
    insert(
        &conn,
        &scope,
        "123",
        "numeric-text-hash",
        "numeric-text-generation",
    );
    let foreign = GraphScope {
        owner: "foreign".into(),
        ..scope.clone()
    };
    insert(
        &conn,
        &foreign,
        "foreign.py",
        "private-hash",
        "private-generation",
    );
    let foreign_workspace = GraphScope {
        workspace_id: "other-workspace".into(),
        ..scope.clone()
    };
    insert(
        &conn,
        &foreign_workspace,
        "workspace-private.py",
        "private-hash",
        "private-generation",
    );
    let original = json!([
        {"rel_path":"same.py","status":"indexed","source_hash":"stale","source_generation":"stale","extra":true},
        {"rel_path":"absent.py","status":"ignored","source_hash":"retained","source_generation":"retained"},
        {"rel_path":"same.py","status":"parse_failed"},
        {"rel_path":"foreign.py"},{"rel_path":"workspace-private.py"},
        {"rel_path":"nul\0é.py"},{"rel_path":123},{"rel_path":"123"},
        {"rel_path":null},{"rel_path":["same.py"]},{"rel_path":{"x":"same.py"}},
        {},[],"not JSON object",null,false,3
    ]);
    let mut expected = original.clone();
    old(&conn, &scope, &mut expected);
    let mut actual = original;
    assert_eq!(
        statements(&conn, || SqliteStore::hydrate_lifecycle_coverage(
            &conn,
            &scope,
            "checkout",
            &mut actual
        )
        .unwrap()),
        1
    );
    assert_eq!(actual, expected);
    assert_eq!(actual[0]["source_generation"], "old-original-generation");
    assert_eq!(actual[1]["source_hash"], "retained");
    assert!(actual[3].get("source_hash").is_none());
    assert!(actual[4].get("source_hash").is_none());
    assert!(actual[6].get("source_hash").is_none());
    for value in [json!([]), json!({"rel_path":"same.py"}), json!(null)] {
        let mut actual = value.clone();
        assert_eq!(
            statements(&conn, || SqliteStore::hydrate_lifecycle_coverage(
                &conn,
                &scope,
                "checkout",
                &mut actual
            )
            .unwrap()),
            0
        );
        assert_eq!(actual, value);
    }
}

#[test]
fn lifecycle_batch_one_statement_for_large_skipped_coverage() {
    let (conn, scope) = fixture();
    for n in 0..129 {
        insert(&conn, &scope, &format!("file-{n}.py"), "hash", "generation");
    }
    let original=Value::Array((0..8659).map(|n|json!({"rel_path":format!("file-{n}.py"),"status":if n<129{"indexed"}else{"ignored"}})).collect());
    let mut expected = original.clone();
    assert_eq!(
        statements(&conn, || old(&conn, &scope, &mut expected)),
        8659
    );
    let mut actual = original;
    assert_eq!(
        statements(&conn, || SqliteStore::hydrate_lifecycle_coverage(
            &conn,
            &scope,
            "checkout",
            &mut actual
        )
        .unwrap()),
        1
    );
    assert_eq!(actual, expected);
    let sql="EXPLAIN QUERY PLAN SELECT j.key,f.blake3,f.source_generation FROM json_each(?1) AS j CROSS JOIN code_files AS f ON f.codebase=?2 AND f.rel_path=json_extract(CASE WHEN j.type='object' THEN j.value ELSE '{}' END,'$.rel_path') WHERE json_type(CASE WHEN j.type='object' THEN j.value ELSE '{}' END,'$.rel_path')='text'";
    let plan = conn
        .prepare(sql)
        .unwrap()
        .query_map(params!["[]", "scope"], |r| r.get::<_, String>(3))
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap();
    assert!(
        plan.iter()
            .any(|s| s.contains("SEARCH f USING INDEX") && s.contains("codebase=? AND rel_path=?")),
        "{plan:?}"
    );
    eprintln!(
        "lifecycle_batch_statements old=8659 new=1 coverage=8659 mapped=129 exact_parity=true"
    );
}
