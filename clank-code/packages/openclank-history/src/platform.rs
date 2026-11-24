//! History host transport. The authenticated protocol is shared across hosts.
use std::{io, path::Path};

#[cfg(unix)]
pub type Stream = tokio::net::UnixStream;
#[cfg(unix)]
pub struct Listener {
    inner: Option<tokio::net::UnixListener>,
    socket: String,
    lock: String,
}
#[cfg(unix)]
impl Drop for Listener {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.socket);
        let _ = std::fs::remove_file(&self.lock);
    }
}
#[cfg(unix)]
impl Listener {
    pub fn bind(socket: &str) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        use std::os::unix::fs::PermissionsExt;
        let lock_path = format!("{socket}.lock");
        if Path::new(socket).exists() {
            let pid = std::fs::read_to_string(&lock_path)
                .ok()
                .and_then(|value| value.trim().parse::<u32>().ok());
            let alive = pid.is_some_and(|pid| {
                std::process::Command::new("kill")
                    .args(["-0", &pid.to_string()])
                    .status()
                    .is_ok_and(|status| status.success())
            });
            if alive || !Path::new(&lock_path).exists() {
                return Err("refusing existing socket path".into());
            }
            std::fs::remove_file(socket)?;
        }
        if Path::new(&lock_path).exists() {
            let pid = std::fs::read_to_string(&lock_path)
                .ok()
                .and_then(|value| value.trim().parse::<u32>().ok());
            let alive = pid.is_some_and(|pid| {
                std::process::Command::new("kill")
                    .args(["-0", &pid.to_string()])
                    .status()
                    .is_ok_and(|status| status.success())
            });
            if alive {
                return Err("history installation is already locked".into());
            }
            std::fs::remove_file(&lock_path)?;
        }
        let mut lock = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock_path)
            .map_err(|_| "history installation is already locked")?;
        let mut guard = Self {
            inner: None,
            socket: socket.to_owned(),
            lock: lock_path.clone(),
        };
        use std::io::Write;
        if std::env::var("OPENCLANK_HISTORY_FAIL_STARTUP")
            .ok()
            .as_deref()
            == Some("pid")
        {
            return Err("injected pid startup failure".into());
        }
        writeln!(lock, "{}", std::process::id())?;
        if std::env::var("OPENCLANK_HISTORY_FAIL_STARTUP")
            .ok()
            .as_deref()
            == Some("bind")
        {
            return Err("injected bind startup failure".into());
        }
        guard.inner = Some(tokio::net::UnixListener::bind(socket)?);
        if std::env::var("OPENCLANK_HISTORY_FAIL_STARTUP")
            .ok()
            .as_deref()
            == Some("chmod")
        {
            return Err("injected chmod startup failure".into());
        }
        std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o600))?;
        Ok(guard)
    }
    pub async fn accept(&mut self) -> io::Result<Stream> {
        self.inner
            .as_ref()
            .expect("bound listener")
            .accept()
            .await
            .map(|(stream, _)| stream)
    }
}

#[cfg(unix)]
pub fn available_bytes(path: &Path) -> Option<u64> {
    let path = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).ok()?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) } != 0 {
        return None;
    }
    let stats = unsafe { stats.assume_init() };
    (stats.f_bavail as u64).checked_mul(stats.f_frsize as u64)
}

#[cfg(unix)]
pub fn sync_directory(path: &Path) -> io::Result<()> {
    std::fs::File::open(path)?.sync_all()
}

#[cfg(windows)]
mod windows {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::ffi::c_void;
    use std::os::windows::{ffi::OsStrExt, fs::OpenOptionsExt};
    use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
    #[repr(C)]
    struct SecurityAttributes {
        length: u32,
        descriptor: *mut c_void,
        inherit: i32,
    }
    #[link(name = "advapi32")]
    unsafe extern "system" {
        fn OpenProcessToken(process: *mut c_void, access: u32, token: *mut *mut c_void) -> i32;
        fn GetTokenInformation(
            token: *mut c_void,
            class: u32,
            buffer: *mut c_void,
            size: u32,
            required: *mut u32,
        ) -> i32;
        fn ConvertSidToStringSidW(sid: *mut c_void, string: *mut *mut u16) -> i32;
        fn SetFileSecurityW(path: *const u16, information: u32, descriptor: *mut c_void) -> i32;
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            s: *const u16,
            revision: u32,
            out: *mut *mut c_void,
            size: *mut u32,
        ) -> i32;
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetCurrentProcess() -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn ReplaceFileW(
            replaced: *const u16,
            replacement: *const u16,
            backup: *const u16,
            flags: u32,
            exclude: *mut c_void,
            reserved: *mut c_void,
        ) -> i32;
        fn MoveFileExW(source: *const u16, destination: *const u16, flags: u32) -> i32;
        fn LocalFree(p: *mut c_void) -> *mut c_void;
        fn GetDiskFreeSpaceExW(
            path: *const u16,
            available: *mut u64,
            total: *mut u64,
            free: *mut u64,
        ) -> i32;
    }
    pub type Stream = NamedPipeServer;
    pub struct Listener {
        name: String,
        pending: NamedPipeServer,
    }
    fn user_sddl(inheritance: &str) -> io::Result<Vec<u16>> {
        let mut token = std::ptr::null_mut();
        if unsafe { OpenProcessToken(GetCurrentProcess(), 0x8, &mut token) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let result = (|| {
            let mut required = 0;
            unsafe {
                GetTokenInformation(token, 1, std::ptr::null_mut(), 0, &mut required);
            }
            if required == 0 {
                return Err(io::Error::last_os_error());
            }
            // TOKEN_USER starts with an aligned SID_AND_ATTRIBUTES pointer.
            let mut buffer = vec![
                0usize;
                (required as usize + std::mem::size_of::<usize>() - 1)
                    / std::mem::size_of::<usize>()
            ];
            if unsafe {
                GetTokenInformation(
                    token,
                    1,
                    buffer.as_mut_ptr() as *mut c_void,
                    required,
                    &mut required,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            let sid = buffer[0] as *mut c_void;
            let mut text = std::ptr::null_mut();
            if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let mut length = 0;
            while unsafe { *text.add(length) } != 0 {
                length += 1;
            }
            let sid_text =
                String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(text, length) });
            unsafe {
                LocalFree(text as *mut c_void);
            }
            Ok(
                format!("D:P(A;{inheritance};GA;;;SY)(A;{inheritance};GA;;;{sid_text})")
                    .encode_utf16()
                    .chain(Some(0))
                    .collect(),
            )
        })();
        unsafe {
            CloseHandle(token);
        }
        result
    }
    fn create(name: &str, first: bool) -> io::Result<NamedPipeServer> {
        // Protected DACL: SYSTEM and the creating user SID only.
        let sddl = user_sddl("")?;
        let mut descriptor = std::ptr::null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let mut attributes = SecurityAttributes {
            length: std::mem::size_of::<SecurityAttributes>() as u32,
            descriptor,
            inherit: 0,
        };
        let result = unsafe {
            ServerOptions::new()
                .first_pipe_instance(first)
                .reject_remote_clients(true)
                .create_with_security_attributes_raw(name, &mut attributes as *mut _ as *mut c_void)
        };
        unsafe {
            LocalFree(descriptor);
        }
        result
    }
    impl Listener {
        pub fn bind(logical: &str) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
            let name = format!(
                r"\\.\pipe\openclank-history-{:x}",
                Sha256::digest(logical.as_bytes())
            );
            let pending = create(&name, true)?;
            Ok(Self { name, pending })
        }
        pub async fn accept(&mut self) -> io::Result<Stream> {
            self.pending.connect().await?;
            // Create next instance before handing off the connected one, so
            // the namespace remains exclusively owned for the full lifetime.
            let next = create(&self.name, false)?;
            Ok(std::mem::replace(&mut self.pending, next))
        }
    }
    pub fn protect_private_directory(path: &Path) -> io::Result<()> {
        use std::os::windows::fs::MetadataExt;
        if std::fs::symlink_metadata(path)?.file_attributes() & 0x400 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "private History directory is a reparse point",
            ));
        }
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let sddl = user_sddl("OICI")?;
        let mut descriptor = std::ptr::null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                1,
                &mut descriptor,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let ok = unsafe { SetFileSecurityW(wide.as_ptr(), 0x80000004, descriptor) };
        let error = io::Error::last_os_error();
        unsafe {
            LocalFree(descriptor);
        }
        if ok == 0 { Err(error) } else { Ok(()) }
    }
    pub fn available_bytes(path: &Path) -> Option<u64> {
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut free = 0;
        if unsafe {
            GetDiskFreeSpaceExW(
                wide.as_ptr(),
                &mut free,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        } == 0
        {
            None
        } else {
            Some(free)
        }
    }
    pub fn publish(source: &Path, destination: &Path) -> io::Result<()> {
        let source_w: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
        let dest_w: Vec<u16> = destination
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect();
        let ok = if destination.exists() {
            // ReplaceFile retains the existing destination's ACL and attributes.
            unsafe {
                ReplaceFileW(
                    dest_w.as_ptr(),
                    source_w.as_ptr(),
                    std::ptr::null(),
                    0,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            }
        } else {
            unsafe { MoveFileExW(source_w.as_ptr(), dest_w.as_ptr(), 0x8) }
        };
        if ok == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }
    pub fn finish_publication(path: &Path) -> io::Result<()> {
        use std::os::windows::fs::MetadataExt;
        let metadata = std::fs::symlink_metadata(path)?;
        if metadata.file_attributes() & 0x400 != 0 {
            // Native symlink metadata has no writable data stream. Open the
            // reparse object itself to validate publication, never its target.
            let _link = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(0x02200000)
                .open(path)?;
            return Ok(());
        }
        if metadata.is_dir() {
            // Windows contract: flush every restored data stream. Namespace
            // recovery uses the persisted restore journal/receipt; this is
            // deliberately not represented as a POSIX directory fsync.
            for entry in std::fs::read_dir(path)? {
                finish_publication(&entry?.path())?;
            }
            return Ok(());
        }
        std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(0x80000000)
            .open(path)?
            .sync_all()
    }
}
#[cfg(windows)]
pub use windows::{
    Listener, Stream, available_bytes, finish_publication, protect_private_directory, publish,
};

#[cfg(unix)]
pub fn protect_private_directory(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
}

#[cfg(unix)]
pub fn publish(source: &Path, destination: &Path) -> io::Result<()> {
    std::fs::rename(source, destination)
}

pub fn symlink(target: &str, destination: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(target, destination)
    }
    #[cfg(windows)]
    {
        // Windows needs link type explicitly; fail for unknown/dangling types
        // rather than silently restoring the wrong kind of native link.
        let resolved = destination.parent().unwrap_or(Path::new(".")).join(target);
        if std::fs::metadata(resolved)?.is_dir() {
            std::os::windows::fs::symlink_dir(target, destination)
        } else {
            std::os::windows::fs::symlink_file(target, destination)
        }
    }
}

#[cfg(unix)]
pub fn finish_publication(path: &Path) -> io::Result<()> {
    sync_directory(
        path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "publication has no parent")
        })?,
    )
}
