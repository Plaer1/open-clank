//! Inert connection-local compatibility marker; no schema guard is installed.
use rusqlite::{functions::FunctionFlags, Connection, Transaction};

pub const WRITER_CAPABILITY: &str = "openclank.frankenmemory.writer.v1";
pub const WRITER_FUNCTION: &str = "fm_writer_capability";

/// Each SQLite database path must name a single file. Independent hard links
/// would otherwise acquire different worker locks and use different WAL files.
#[cfg(windows)]
pub fn require_single_database_link(path: &std::path::Path) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION};
    let file = std::fs::File::open(path)?;
    let mut information = std::mem::MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::uninit();
    // The file handle lives through the call, which initializes the structure on success.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { information.assume_init() }.nNumberOfLinks != 1 {
        return Err(std::io::Error::other("hard-linked Memery databases cannot share a SQLite WAL safely"));
    }
    Ok(())
}

/// Unsupported legacy semantic writers must not cross the guarded epoch.
/// Call inside their own write transaction, never as a caller-provided flag.
pub fn require_legacy_semantic_epoch(tx: &Transaction<'_>) -> rusqlite::Result<()> {
    let version: i64 = tx.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if version != 16 { return Err(rusqlite::Error::InvalidQuery); }
    Ok(())
}

pub fn register_writer_capability(conn: &Connection) -> rusqlite::Result<()> {
    conn.create_scalar_function(
        WRITER_FUNCTION,
        0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
        |_| Ok(WRITER_CAPABILITY),
    )
}


thread_local! { static DOMAIN_METADATA_WRITE: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) }; }

/// Only synchronous, already-authenticated private review transactions may enter.
pub(crate) fn with_domain_metadata_authority<T>(conn: &mut Connection, f: impl FnOnce(&mut Connection) -> T) -> T {
    struct Reset;
    impl Drop for Reset { fn drop(&mut self) { DOMAIN_METADATA_WRITE.with(|value| value.set(None)); } }
    // The connection remains borrowed for the entire scope; its SQLite handle
    // cannot be closed or reused while this identity is installed.
    let identity=unsafe { conn.handle() } as usize;
    DOMAIN_METADATA_WRITE.with(|value| { assert!(value.get().is_none(), "nested domain metadata authority"); value.set(Some(identity)); });
    let _reset=Reset; f(conn)
}

thread_local! { static S02_POLICY_WRITE: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) }; }

/// Enter only around a synchronous, independently authorized S02 transaction.
pub(crate) fn with_s02_policy_authority<T>(conn: &mut Connection, f: impl FnOnce(&mut Connection) -> T) -> T {
    struct Reset;
    impl Drop for Reset { fn drop(&mut self) { S02_POLICY_WRITE.with(|value| value.set(None)); } }
    let identity = unsafe { conn.handle() } as usize;
    S02_POLICY_WRITE.with(|value| { assert!(value.get().is_none(), "nested S02 policy authority"); value.set(Some(identity)); });
    let _reset = Reset; f(conn)
}

thread_local! { static PAIR_PENDING_REBIND: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) }; }

/// Enter only around the synchronous, independently verified pending-pair CAS.
pub(crate) fn with_pair_pending_rebind_authority<T>(conn: &mut Connection, f: impl FnOnce(&mut Connection) -> T) -> T {
    struct Reset;
    impl Drop for Reset { fn drop(&mut self) { PAIR_PENDING_REBIND.with(|value| value.set(None)); } }
    let identity=unsafe { conn.handle() } as usize;
    PAIR_PENDING_REBIND.with(|value| { assert!(value.get().is_none(), "nested pending pair authority"); value.set(Some(identity)); });
    let _reset=Reset; f(conn)
}

thread_local! {
    static DOCUMENT_VECTOR_BATCH: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
    static DOCUMENT_EPOCH_INIT: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
    static DOCUMENT_EPOCH_LIFECYCLE: std::cell::Cell<Option<usize>> = const { std::cell::Cell::new(None) };
}

/// Enter only around a synchronous, independently authenticated v2 batch transaction.
pub(crate) fn with_document_vector_batch_authority<T>(conn:&Connection,f:impl FnOnce()->T)->T {
    struct Reset; impl Drop for Reset {fn drop(&mut self){DOCUMENT_VECTOR_BATCH.with(|v|v.set(None));}}
    let identity=unsafe {conn.handle()} as usize;
    DOCUMENT_VECTOR_BATCH.with(|v|{assert!(v.get().is_none(),"nested document vector batch authority");v.set(Some(identity));});
    let _reset=Reset;f()
}
/// Epoch initialization is allowed only alongside the original verified snapshot.
pub(crate) fn with_document_epoch_init_authority<T>(conn:&Connection,f:impl FnOnce()->T)->T {
    struct Reset; impl Drop for Reset {fn drop(&mut self){DOCUMENT_EPOCH_INIT.with(|v|v.set(None));}}
    let identity=unsafe {conn.handle()} as usize;
    DOCUMENT_EPOCH_INIT.with(|v|{assert!(v.get().is_none(),"nested document epoch initialization");v.set(Some(identity));});
    let _reset=Reset;f()
}
/// Only the exact guarded rename/purge owner may remove an old epoch key.
pub(crate) fn with_document_epoch_lifecycle_authority<T>(conn:&Connection,f:impl FnOnce()->T)->T {
    struct Reset; impl Drop for Reset {fn drop(&mut self){DOCUMENT_EPOCH_LIFECYCLE.with(|v|v.set(None));}}
    let identity=unsafe {conn.handle()} as usize;
    DOCUMENT_EPOCH_LIFECYCLE.with(|v|{assert!(v.get().is_none(),"nested document epoch lifecycle");v.set(Some(identity));});
    let _reset=Reset;f()
}

/// Installed only on an explicitly admitted guarded connection.
pub(crate) fn register_guarded_writer_capability(conn: &Connection) -> rusqlite::Result<()> {
    register_guarded_writer_capability_for_epoch(conn, 17)
}

pub(crate) fn register_guarded_writer_capability_for_epoch(conn: &Connection, epoch:i64) -> rusqlite::Result<()> {
    let marker = match epoch { 17 => "openclank.frankenmemory.writer.v2", 18 => "openclank.frankenmemory.writer.v3", _ => return Err(rusqlite::Error::InvalidQuery) };
    conn.create_scalar_function(WRITER_FUNCTION, 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(marker))?;
    conn.create_scalar_function("fm_policy_projection_capability", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
        |_| Ok("openclank.policy_projection.writer.v1"))?;
    conn.create_scalar_function("fm_policy_s08_review_capability", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
        |_| Ok("openclank.policy_projection.review.v1"))?;
    conn.create_scalar_function("fm_queued_source_capability", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
        |_| Ok("openclank.queued_source.writer.v1"))?;
    let domain_connection=unsafe { conn.handle() } as usize;
    conn.create_scalar_function("fm_domain_metadata_authorized", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(DOMAIN_METADATA_WRITE.with(|value| if value.get()==Some(domain_connection) {1_i64} else {0_i64})))?;
    let s02_connection = unsafe { conn.handle() } as usize;
    conn.create_scalar_function("fm_s02_policy_authorized", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(S02_POLICY_WRITE.with(|value| if value.get()==Some(s02_connection) {1_i64} else {0_i64})))?;
    let pair_connection=unsafe { conn.handle() } as usize;
    conn.create_scalar_function("fm_pair_pending_rebind_authorized", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(PAIR_PENDING_REBIND.with(|value| if value.get()==Some(pair_connection) {1_i64} else {0_i64})))?;
    let document_connection=unsafe { conn.handle() } as usize;
    conn.create_scalar_function("fm_document_vector_batch_authorized",0,FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(DOCUMENT_VECTOR_BATCH.with(|v|if v.get()==Some(document_connection) {1_i64} else {0_i64})))?;
    conn.create_scalar_function("fm_document_epoch_init_authorized",0,FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(DOCUMENT_EPOCH_INIT.with(|v|if v.get()==Some(document_connection) {1_i64} else {0_i64})))?;
    conn.create_scalar_function("fm_document_epoch_lifecycle_authorized",0,FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_INNOCUOUS,
        move |_| Ok(DOCUMENT_EPOCH_LIFECYCLE.with(|v|if v.get()==Some(document_connection) {1_i64} else {0_i64})))?;
    conn.create_scalar_function("fm_s09_question_capability", 0,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC | FunctionFlags::SQLITE_INNOCUOUS,
        |_| Ok("openclank.questions.writer.v1"))
}

/// The connection marker must match the exact already-verified guarded epoch.
pub fn require_matched_guarded_epoch(tx: &Connection) -> rusqlite::Result<()> {
    let epoch:i64=tx.query_row("PRAGMA user_version", [], |r|r.get(0))?;
    let trusted:i64=tx.query_row("PRAGMA trusted_schema", [], |r|r.get(0))?;
    if trusted!=1 {return Err(rusqlite::Error::InvalidQuery);}
    let marker:String=tx.query_row("SELECT fm_writer_capability()", [], |r|r.get(0))?;
    if !matches!((epoch,marker.as_str()), (17,"openclank.frankenmemory.writer.v2") | (18,"openclank.frankenmemory.writer.v3")) { return Err(rusqlite::Error::InvalidQuery); }
    Ok(())
}
