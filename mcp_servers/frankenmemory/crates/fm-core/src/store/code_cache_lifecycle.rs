//! Explicit erasure of disposable code-index generations after durable metadata
//! erasure commits. A busy/unsafe cache is an honest incomplete cleanup, never a
//! reason to restore erased metadata or unlink a pinned reader's generation.
use super::code_partitioned::{directory_owner, directory_scope, directory_workspace, lock, CacheDirectory};
use crate::graph::GraphScope;
use rusqlite::Connection;
use std::fs::File;

const MAX_CLEANUP_ENTRIES: usize = 100_000;

fn pending(error: impl std::fmt::Display) -> String {
    format!("cache_cleanup_pending: code metadata transition committed; derived cache cleanup incomplete ({error}); retry explicit erasure after readers and writers close")
}

pub(crate) fn remove_scope_cache(conn: &Connection, scope: &GraphScope, checkout: &str) -> Result<(), String> {
    clean(directory_scope(conn, scope, checkout, false), 0)
}

pub(crate) fn remove_workspace_cache(conn: &Connection, scope: &GraphScope) -> Result<(), String> {
    clean(directory_workspace(conn, scope, false), 1)
}

pub(crate) fn remove_owner_cache(conn: &Connection, owner: &str) -> Result<(), String> {
    clean(directory_owner(conn, owner, false), 2)
}

fn clean(directory: Result<Option<CacheDirectory>, String>, depth: usize) -> Result<(), String> {
    let Some(directory) = directory.map_err(pending)? else { return Ok(()); };
    // Generation holders retain SH locks along the namespace hierarchy. EX on
    // this exact target also excludes creation below it during enumeration.
    lock(&directory.pin, true).map_err(pending)?;
    let mut remaining = MAX_CLEANUP_ENTRIES;
    clear(&directory.pin, depth, &mut remaining).map_err(pending)
}

fn generation_name(name: &str) -> bool {
    name.strip_prefix("generation_").and_then(|value| value.strip_suffix(".sqlite"))
        .is_some_and(|id| id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()))
}

#[cfg(unix)]
pub(crate) fn directory_names(directory: &File) -> Result<Vec<std::ffi::OsString>, String> {
    use std::os::{fd::AsRawFd, unix::ffi::OsStringExt};
    // Open dot relative to the pinned directory for an independent directory
    // offset. /dev/fd directory paths are not traversable on macOS; dup alone
    // would also share offsets with another enumeration of the same handle.
    let fd = unsafe { libc::openat(directory.as_raw_fd(), c".".as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 { return Err(std::io::Error::last_os_error().to_string()); }
    let stream = unsafe { libc::fdopendir(fd) };
    if stream.is_null() {
        let error = std::io::Error::last_os_error(); unsafe { libc::close(fd); }
        return Err(error.to_string());
    }
    struct Directory(*mut libc::DIR);
    impl Drop for Directory { fn drop(&mut self) { unsafe { libc::closedir(self.0); } } }
    let stream = Directory(stream); let mut names = Vec::new();
    loop {
        // POSIX readdir distinguishes EOF from an error through errno.
        #[cfg(any(target_os = "macos", target_os = "ios", target_os = "freebsd"))]
        let errno = unsafe { libc::__error() };
        #[cfg(any(target_os = "linux", target_os = "android"))]
        let errno = unsafe { libc::__errno_location() };
        #[cfg(not(any(target_os = "macos", target_os = "ios", target_os = "freebsd", target_os = "linux", target_os = "android")))]
        compile_error!("descriptor directory enumeration needs the platform errno accessor");
        unsafe { *errno = 0; }
        let item = unsafe { libc::readdir(stream.0) };
        if item.is_null() {
            let code = unsafe { *errno };
            if code != 0 { return Err(std::io::Error::from_raw_os_error(code).to_string()); }
            break;
        }
        let bytes = unsafe { std::ffi::CStr::from_ptr((*item).d_name.as_ptr()) }.to_bytes();
        if bytes == b"." || bytes == b".." { continue; }
        if names.len() >= MAX_CLEANUP_ENTRIES { return Err("cache directory entry budget exceeded".into()); }
        names.push(std::ffi::OsString::from_vec(bytes.to_vec()));
    }
    Ok(names)
}

#[cfg(not(unix))]
pub(crate) fn directory_names(_directory: &File) -> Result<Vec<std::ffi::OsString>, String> {
    Err("descriptor-backed cache enumeration is unavailable on this platform".into())
}

#[cfg(unix)]
fn clear(directory: &File, depth: usize, remaining: &mut usize) -> Result<(), String> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::fs::MetadataExt;
    let failure = |error: std::io::Error| error.to_string();
    // Enumerate through the pinned directory, never a newly resolved pathname.
    let mut names = Vec::new();
    for item in directory_names(directory)? {
        if *remaining == 0 { return Err("cache cleanup entry budget exceeded".into()); }
        *remaining -= 1;
        let name = item.into_string().map_err(|_| "non-UTF8 cache entry")?;
        let valid = if depth == 0 { generation_name(&name) }
            else { name.len() == 64 && name.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) };
        if !valid { return Err("unexpected cache entry retained".into()); }
        names.push(name);
    }
    for name in names {
        let c_name = CString::new(name).map_err(|_| "invalid cache entry")?;
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC
            | if depth > 0 { libc::O_DIRECTORY } else { 0 };
        let fd = unsafe { libc::openat(directory.as_raw_fd(), c_name.as_ptr(), flags) };
        if fd < 0 { return Err(failure(std::io::Error::last_os_error())); }
        let pin = unsafe { File::from_raw_fd(fd) };
        let meta = pin.metadata().map_err(failure)?;
        if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0
            || (depth > 0 && !meta.is_dir()) || (depth == 0 && !meta.is_file()) {
            return Err("unsafe cache entry retained".into());
        }
        lock(&pin, true)?;
        if depth > 0 {
            clear(&pin, depth - 1, remaining)?;
        } else {
            // Verify that the entry still names our locked inode. Never read
            // payload bytes: even a corrupt oversized cache can be erased.
            let mut current = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe { libc::fstatat(directory.as_raw_fd(), c_name.as_ptr(), current.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } != 0 {
                return Err(failure(std::io::Error::last_os_error()));
            }
            let current = unsafe { current.assume_init() };
            if current.st_dev as u64 != meta.dev() || current.st_ino as u64 != meta.ino()
                || current.st_mode as u32 & libc::S_IFMT as u32 != libc::S_IFREG as u32 {
                return Err("cache entry identity changed during cleanup".into());
            }
            if unsafe { libc::unlinkat(directory.as_raw_fd(), c_name.as_ptr(), 0) } != 0 {
                return Err(failure(std::io::Error::last_os_error()));
            }
        }
    }
    // Keep empty namespace directories: removing them can detach another
    // participant's pinned directory. They contain no retained code payload.
    directory.sync_all().map_err(failure)
}

#[cfg(not(unix))]
fn clear(_directory: &File, _depth: usize, _remaining: &mut usize) -> Result<(), String> {
    Err("descriptor-backed cache cleanup is unavailable on this platform".into())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{fs::{self, OpenOptions}, path::PathBuf};
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, symlink};
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("fm-cache-cleanup-{}", uuid::Uuid::new_v4().simple()));
            fs::DirBuilder::new().mode(0o700).create(&path).unwrap(); Self(path)
        }
        fn dir(&self, name: &str) -> PathBuf {
            let path = self.0.join(name); fs::DirBuilder::new().recursive(true).mode(0o700).create(&path).unwrap(); path
        }
        fn file(path: &std::path::Path) -> File { OpenOptions::new().read(true).write(true).create_new(true).mode(0o600).open(path).unwrap() }
    }
    impl Drop for Fixture { fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); } }
    fn name(n: u8) -> String { format!("generation_{n:032x}.sqlite") }
    fn erase(path: &std::path::Path, depth: usize) -> Result<(), String> {
        let pin = File::open(path).unwrap(); lock(&pin, true)?; clear(&pin, depth, &mut 100)
    }
    #[test]
    fn cleanup_is_scoped_idempotent_and_removes_oversized_corrupt_payload_without_reading() {
        let root = Fixture::new();
        let own = root.dir(&format!("{}/{}/{}", "a".repeat(64), "b".repeat(64), "c".repeat(64)));
        let other = root.dir(&format!("{}/{}/{}", "d".repeat(64), "b".repeat(64), "c".repeat(64)));
        Fixture::file(&own.join(name(1))).set_len(super::super::code_partitioned::MAX_DISK + 1).unwrap();
        Fixture::file(&other.join(name(1)));
        erase(&root.0.join("a".repeat(64)), 2).unwrap();
        assert!(own.is_dir()); assert!(!own.join(name(1)).exists()); assert!(other.join(name(1)).exists());
        erase(&root.0.join("a".repeat(64)), 2).unwrap();
    }
    #[test]
    fn active_file_and_namespace_pins_block_cleanup_until_released() {
        let root = Fixture::new(); let file = Fixture::file(&root.0.join(name(1)));
        lock(&file, false).unwrap(); assert!(erase(&root.0, 0).is_err()); assert!(root.0.join(name(1)).exists());
        drop(file);
        let namespace = File::open(&root.0).unwrap(); lock(&namespace, false).unwrap();
        assert!(erase(&root.0, 0).is_err()); drop(namespace);
        erase(&root.0, 0).unwrap(); assert!(!root.0.join(name(1)).exists());
    }
    #[test]
    fn symlink_fifo_and_unknown_entries_are_retained_without_traversal() {
        let root = Fixture::new(); let outside = Fixture::new(); let target = outside.0.join("keep"); Fixture::file(&target);
        symlink(&target, root.0.join(name(1))).unwrap(); assert!(erase(&root.0, 0).is_err()); assert!(target.exists());
        fs::remove_file(root.0.join(name(1))).unwrap();
        let fifo = std::ffi::CString::new(root.0.join(name(1)).to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
        assert!(erase(&root.0, 0).is_err()); fs::remove_file(root.0.join(name(1))).unwrap();
        Fixture::file(&root.0.join("unknown.sqlite")); assert!(erase(&root.0, 0).is_err());
        assert!(root.0.join("unknown.sqlite").exists());
    }
    #[test]
    fn nested_symlink_and_entry_budget_fail_closed() {
        let root = Fixture::new(); let outside = Fixture::new(); Fixture::file(&outside.0.join(name(1)));
        symlink(&outside.0, root.0.join("a".repeat(64))).unwrap();
        assert!(erase(&root.0, 1).is_err()); assert!(outside.0.join(name(1)).exists());
        let file_root = Fixture::new(); Fixture::file(&file_root.0.join(name(1)));
        assert!(clear(&File::open(&file_root.0).unwrap(), 0, &mut 0).is_err());
        assert!(file_root.0.join(name(1)).exists());
    }
    #[test]
    fn in_memory_legacy_store_needs_no_cache_or_home_lookup() {
        let conn = Connection::open_in_memory().unwrap(); let scope = GraphScope::new("a", "w").unwrap();
        remove_scope_cache(&conn, &scope, "c").unwrap(); remove_workspace_cache(&conn, &scope).unwrap(); remove_owner_cache(&conn, "a").unwrap();
    }

    const CHILD_TEST: &str = "store::code_cache_lifecycle::tests::isolated_cleanup_child";
    struct Child(std::process::Child);
    impl Child {
        fn wait(&mut self) -> std::process::ExitStatus {
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
            loop {
                if let Some(status) = self.0.try_wait().unwrap() { return status; }
                assert!(std::time::Instant::now() < deadline, "owned cleanup test child timed out");
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
        }
        fn release(&mut self) {
            use std::io::Write;
            self.0.stdin.as_mut().unwrap().write_all(b"x").unwrap();
            self.0.stdin.take();
            assert!(self.wait().success());
        }
    }
    impl Drop for Child {
        fn drop(&mut self) {
            // EOF releases a holder even when an assertion panics. Terminate
            // only this owned fixture child if it does not exit promptly.
            self.0.stdin.take();
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
            while self.0.try_wait().ok().flatten().is_none() {
                if std::time::Instant::now() >= deadline {
                    let _ = self.0.kill(); let _ = self.0.wait(); break;
                }
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
        }
    }
    fn child_command() -> std::process::Command {
        let mut command = std::process::Command::new(std::env::current_exe().unwrap());
        command.args(["--exact", CHILD_TEST, "--nocapture", "--test-threads=1"])
            .env_remove("FM_CLEANUP_CHILD_MODE").env_remove("FM_CLEANUP_CHILD_ROOT")
            .env_remove("FM_CLEANUP_CHILD_PIN").env_remove("FM_CLEANUP_CHILD_READY")
            .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::null());
        command
    }
    fn hold_in_child(path: &std::path::Path, root: &Fixture) -> Child {
        let ready = root.0.join(format!("ready-{}", uuid::Uuid::new_v4()));
        let mut child = Child(child_command().env("FM_CLEANUP_CHILD_MODE", "pin")
            .env("FM_CLEANUP_CHILD_PIN", path).env("FM_CLEANUP_CHILD_READY", &ready).spawn().unwrap());
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while !ready.exists() {
            assert!(child.0.try_wait().unwrap().is_none(), "pin holder exited before admission");
            assert!(std::time::Instant::now() < deadline, "pin holder failed to become ready");
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        fs::remove_file(ready).unwrap(); child
    }
    #[test]
    fn isolated_cleanup_child() {
        use std::io::Read;
        let Ok(mode) = std::env::var("FM_CLEANUP_CHILD_MODE") else { return; };
        if mode == "pin" {
            let file = File::open(std::env::var_os("FM_CLEANUP_CHILD_PIN").unwrap()).unwrap();
            lock(&file, false).unwrap();
            fs::write(std::env::var_os("FM_CLEANUP_CHILD_READY").unwrap(), b"ready").unwrap();
            let mut byte = [0]; let _ = std::io::stdin().read(&mut byte).unwrap();
            drop(file); return;
        }
        assert_eq!(mode, "postcommit");
        let root = PathBuf::from(std::env::var_os("FM_CLEANUP_CHILD_ROOT").unwrap());
        // HOME is changed only in this subprocess, never in the parent test
        // process or another test's cache namespace.
        assert_eq!(PathBuf::from(std::env::var_os("HOME").unwrap()), root.join("home"));
        let source = root.join("source"); fs::create_dir(&source).unwrap();
        fs::write(source.join("main.py"), "def target():\n    return 1\n").unwrap();
        let store = super::super::sqlite::SqliteStore::new(root.join("main.sqlite").to_str().unwrap(), 4).unwrap();
        let scope = GraphScope::new("cleanup-owner", "cleanup-workspace").unwrap();
        let indexed = store.code_index(&scope, &source).unwrap();
        assert_eq!(indexed.files_indexed, 1);
        assert!(store.code_index_head(&scope, &indexed.codebase).unwrap().is_some());
        let cache = {
            let conn = store.conn.lock().unwrap();
            directory_scope(&conn, &scope, &indexed.codebase, true).unwrap().unwrap().path
        };
        let payload = cache.join(name(1)); Fixture::file(&payload);
        let communication = Fixture::new(); let mut holder = hold_in_child(&payload, &communication);
        let error = store.code_remove(&scope, &indexed.codebase).unwrap_err();
        assert!(error.starts_with("cache_cleanup_pending:"), "{error}");
        assert!(error.contains("metadata transition committed"));
        assert!(payload.exists(), "a held generation must not be unlinked");
        assert!(store.code_index_head(&scope, &indexed.codebase).unwrap().is_none());
        {
            let conn = store.conn.lock().unwrap();
            assert_eq!(conn.query_row("SELECT count(*) FROM code_files", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
            assert_eq!(conn.query_row("SELECT lifecycle FROM code_registrations WHERE checkout_id=?1", [&indexed.codebase], |r| r.get::<_, String>(0)).unwrap(), "erased");
            assert_eq!(conn.query_row("SELECT count(*) FROM code_index_runs", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
        }
        holder.release();
        assert_eq!(store.code_remove(&scope, &indexed.codebase).unwrap(), 0);
        assert!(!payload.exists());
        assert!(store.code_index_head(&scope, &indexed.codebase).unwrap().is_none());
    }
    #[test]
    fn cross_process_namespace_and_file_pins_report_pending_then_retry() {
        let root = Fixture::new(); let cache = root.dir("cache"); let payload = cache.join(name(1));
        Fixture::file(&payload);
        for path in [&cache, &payload] {
            let mut holder = hold_in_child(path, &root);
            let erase = || clean(Ok(Some(CacheDirectory { path: cache.clone(), pin: File::open(&cache).unwrap(), guards: vec![] })), 0);
            let error = erase().unwrap_err();
            assert!(error.starts_with("cache_cleanup_pending:"), "{error}");
            assert!(payload.exists()); holder.release();
            if path == &payload { erase().unwrap(); assert!(!payload.exists()); }
        }
    }
    #[test]
    fn code_remove_commits_before_busy_cache_cleanup_and_retry_is_idempotent() {
        let root = Fixture::new(); fs::create_dir(root.0.join("home")).unwrap();
        let mut child = Child(child_command().env("FM_CLEANUP_CHILD_MODE", "postcommit")
            .env("FM_CLEANUP_CHILD_ROOT", &root.0).env("HOME", root.0.join("home")).spawn().unwrap());
        assert!(child.wait().success(), "isolated postcommit scenario failed");
    }
}
