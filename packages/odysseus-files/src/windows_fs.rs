//! Windows filesystem authority. All identities and final paths come from handles.
//! Native failures are propagated; unsupported identity/ACL facilities never pass.
use std::ffi::{c_void, OsString};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::ptr::null_mut;
type Handle = *mut c_void;
#[repr(C)]
#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct FileIdInfo {
    volume: u64,
    id: [u8; 16],
}
#[repr(C)]
#[derive(Default)]
struct BasicInfo {
    creation: i64,
    access: i64,
    write: i64,
    change: i64,
    attributes: u32,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OpenedIdentity {
    pub volume: u64,
    pub file_id: [u8; 16],
    pub change_time: i64,
}
#[repr(C)]
struct SecurityAttributes {
    length: u32,
    descriptor: Handle,
    inherit: i32,
}
#[link(name = "kernel32")]
extern "system" {
    fn GetFileInformationByHandleEx(handle: Handle, class: i32, buffer: Handle, size: u32) -> i32;
    fn GetFinalPathNameByHandleW(handle: Handle, buffer: *mut u16, length: u32, flags: u32) -> u32;
    fn CreateDirectoryW(path: *const u16, security: *const SecurityAttributes) -> i32;
    fn ReplaceFileW(
        path: *const u16,
        replacement: *const u16,
        backup: *const u16,
        flags: u32,
        exclude: Handle,
        reserved: Handle,
    ) -> i32;
    fn MoveFileExW(source: *const u16, destination: *const u16, flags: u32) -> i32;
    fn LocalFree(memory: Handle) -> Handle;
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
}
#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
    fn GetTokenInformation(
        token: Handle,
        class: i32,
        buffer: Handle,
        size: u32,
        needed: *mut u32,
    ) -> i32;
    fn ConvertSidToStringSidW(sid: Handle, value: *mut *mut u16) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        value: *const u16,
        revision: u32,
        descriptor: *mut Handle,
        size: *mut u32,
    ) -> i32;
    fn GetSecurityInfo(
        handle: Handle,
        kind: i32,
        info: u32,
        owner: *mut Handle,
        group: *mut Handle,
        dacl: *mut Handle,
        sacl: *mut Handle,
        descriptor: *mut Handle,
    ) -> u32;
    fn GetSecurityDescriptorDacl(
        descriptor: Handle,
        present: *mut i32,
        dacl: *mut Handle,
        defaulted: *mut i32,
    ) -> i32;
    fn SetSecurityInfo(
        handle: Handle,
        kind: i32,
        info: u32,
        owner: Handle,
        group: Handle,
        dacl: Handle,
        sacl: Handle,
    ) -> u32;
    fn EqualSid(left: Handle, right: Handle) -> i32;
}
struct LocalAllocation(Handle);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}
struct Token(Handle);
impl Drop for Token {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
fn wide(path: &Path) -> io::Result<Vec<u16>> {
    let mut value: Vec<u16> = path.as_os_str().encode_wide().collect();
    if value.contains(&0) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "path contains NUL",
        ));
    }
    value.push(0);
    Ok(value)
}
fn succeeded(value: i32) -> io::Result<()> {
    if value == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}
pub fn opened_identity(file: &File) -> io::Result<OpenedIdentity> {
    let mut id = FileIdInfo::default();
    let mut basic = BasicInfo::default();
    unsafe {
        succeeded(GetFileInformationByHandleEx(
            file.as_raw_handle(),
            18,
            &mut id as *mut _ as Handle,
            std::mem::size_of::<FileIdInfo>() as u32,
        ))?;
        succeeded(GetFileInformationByHandleEx(
            file.as_raw_handle(),
            0,
            &mut basic as *mut _ as Handle,
            std::mem::size_of::<BasicInfo>() as u32,
        ))?;
    }
    Ok(OpenedIdentity {
        volume: id.volume,
        file_id: id.id,
        change_time: basic.change,
    })
}
pub fn final_path(file: &File) -> io::Result<PathBuf> {
    let mut buffer = vec![0u16; 512];
    loop {
        let length = unsafe {
            GetFinalPathNameByHandleW(
                file.as_raw_handle(),
                buffer.as_mut_ptr(),
                buffer.len() as u32,
                0,
            )
        };
        if length == 0 {
            return Err(io::Error::last_os_error());
        }
        if (length as usize) < buffer.len() {
            buffer.truncate(length as usize);
            return Ok(PathBuf::from(OsString::from_wide(&buffer)));
        }
        if length > 32768 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "final path exceeds native limit",
            ));
        }
        buffer.resize(length as usize + 1, 0);
    }
}
/// Require exact canonical spelling, retaining NTFS case-sensitive directory semantics.
pub fn validate_opened_path(file: &File, canonical: &Path) -> io::Result<()> {
    if final_path(file)? != canonical || fs::canonicalize(canonical)? != canonical {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "opened object escaped its authorized path",
        ));
    }
    let current = File::open(canonical)?;
    if opened_identity(file)? != opened_identity(&current)? {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "opened object was replaced",
        ));
    }
    Ok(())
}
pub fn directory_identity(path: &Path) -> io::Result<OpenedIdentity> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(0x02000000)
        .open(path)?;
    opened_identity(&file)
}
/// Create with a protected inheritable DACL, and reject an existing foreign-owned or reparse root.
/// Children inherit access for the process user and SYSTEM only.
pub fn ensure_private_directory(path: &Path) -> io::Result<()> {
    unsafe {
        let mut token = null_mut();
        succeeded(OpenProcessToken(GetCurrentProcess(), 8, &mut token))?;
        let token = Token(token);
        let mut needed = 0;
        GetTokenInformation(token.0, 1, null_mut(), 0, &mut needed);
        if needed == 0 {
            return Err(io::Error::last_os_error());
        }
        // TokenUser starts with SID_AND_ATTRIBUTES; usize allocation provides pointer alignment.
        let mut user = vec![
            0usize;
            (needed as usize + std::mem::size_of::<usize>() - 1)
                / std::mem::size_of::<usize>()
        ];
        succeeded(GetTokenInformation(
            token.0,
            1,
            user.as_mut_ptr() as Handle,
            needed,
            &mut needed,
        ))?;
        let sid = *(user.as_ptr() as *const Handle);
        let mut sid_text = null_mut();
        succeeded(ConvertSidToStringSidW(sid, &mut sid_text))?;
        let sid_allocation = LocalAllocation(sid_text as Handle);
        let mut length = 0;
        while *sid_text.add(length) != 0 {
            length += 1;
        }
        let sid_string = String::from_utf16_lossy(std::slice::from_raw_parts(sid_text, length));
        let sddl: Vec<u16> =
            format!("O:{sid_string}D:P(A;OICI;FA;;;{sid_string})(A;OICI;FA;;;SY)\0")
                .encode_utf16()
                .collect();
        let mut descriptor = null_mut();
        succeeded(ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            null_mut(),
        ))?;
        let descriptor = LocalAllocation(descriptor);
        drop(sid_allocation);
        let security = SecurityAttributes {
            length: std::mem::size_of::<SecurityAttributes>() as u32,
            descriptor: descriptor.0,
            inherit: 0,
        };
        if CreateDirectoryW(wide(path)?.as_ptr(), &security) == 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(183) {
                return Err(error);
            }
        }
        // No-follow handle prevents changing an ACL on a junction's target. Do not share delete.
        let file = OpenOptions::new()
            .access_mode(0x00020000 | 0x00040000)
            .share_mode(3)
            .custom_flags(0x02000000 | 0x00200000)
            .open(path)?;
        use std::os::windows::fs::MetadataExt;
        let metadata = file.metadata()?;
        if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "private staging root is a reparse point or not a directory",
            ));
        }
        let mut owner = null_mut();
        let mut old_descriptor = null_mut();
        let status = GetSecurityInfo(
            file.as_raw_handle(),
            1,
            1,
            &mut owner,
            null_mut(),
            null_mut(),
            null_mut(),
            &mut old_descriptor,
        );
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status as i32));
        }
        let _old_descriptor = LocalAllocation(old_descriptor);
        if EqualSid(owner, sid) == 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "private staging root has a different owner",
            ));
        }
        let mut present = 0;
        let mut defaulted = 0;
        let mut dacl = null_mut();
        succeeded(GetSecurityDescriptorDacl(
            descriptor.0,
            &mut present,
            &mut dacl,
            &mut defaulted,
        ))?;
        if present == 0 || dacl.is_null() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "private staging DACL is absent",
            ));
        }
        let status = SetSecurityInfo(
            file.as_raw_handle(),
            1,
            4 | 0x80000000,
            null_mut(),
            null_mut(),
            dacl,
            null_mut(),
        );
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status as i32));
        }
    }
    Ok(())
}
/// ReplaceFile preserves the destination ACL/streams. Never ignore ACL merge errors.
/// A backup protects the original in documented partial-failure states; failed backups remain recoverable.
pub fn publish_file(temporary: &Path, destination: &Path, replace: bool) -> io::Result<()> {
    let source = wide(temporary)?;
    let target = wide(destination)?;
    if !replace {
        return succeeded(unsafe { MoveFileExW(source.as_ptr(), target.as_ptr(), 8) });
    }
    let backup = temporary.with_extension("odysseus-recovery");
    if backup.exists() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "save recovery path exists",
        ));
    }
    succeeded(unsafe {
        ReplaceFileW(
            target.as_ptr(),
            source.as_ptr(),
            wide(&backup)?.as_ptr(),
            0,
            null_mut(),
            null_mut(),
        )
    })?;
    // A successful save remains successful if cleanup is denied; the original is recoverable.
    let _ = fs::remove_file(backup);
    Ok(())
}

/// Keep each canonical parent directory immovable for the lifetime of an I/O operation.
/// No-follow opens plus final-path checks reject ancestor reparse substitution.
pub fn pin_parent(path: &Path) -> io::Result<Vec<File>> {
    use std::os::windows::fs::MetadataExt;
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "path lacks parent"))?;
    let mut parents: Vec<_> = parent.ancestors().collect();
    parents.reverse();
    let mut pinned = Vec::with_capacity(parents.len());
    for parent in parents {
        if parent.as_os_str().is_empty() {
            continue;
        }
        let directory = OpenOptions::new()
            .access_mode(0x80)
            .share_mode(3)
            .custom_flags(0x02000000 | 0x00200000)
            .open(parent)?;
        let metadata = directory.metadata()?;
        if !metadata.is_dir()
            || metadata.file_attributes() & 0x400 != 0
            || final_path(&directory)? != parent
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "authorized ancestor changed or is a reparse point",
            ));
        }
        pinned.push(directory);
    }
    Ok(pinned)
}

pub fn pin_directory(path: &Path) -> io::Result<File> {
    use std::os::windows::fs::MetadataExt;
    let directory = OpenOptions::new()
        .access_mode(0x80)
        .share_mode(3)
        .custom_flags(0x02000000 | 0x00200000)
        .open(path)?;
    let metadata = directory.metadata()?;
    if !metadata.is_dir()
        || metadata.file_attributes() & 0x400 != 0
        || final_path(&directory)? != path
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "authorized directory changed or is a reparse point",
        ));
    }
    Ok(directory)
}

/// Compare DOS/UNC and their verbatim aliases without folding filename case or changing I/O paths.
/// Python root snapshots use DOS spelling while Rust canonicalize returns verbatim spelling.
pub(crate) fn authority_path(path: &Path) -> PathBuf {
    use std::path::{Component, Prefix};
    let mut result = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => match prefix.kind() {
                Prefix::Disk(drive) | Prefix::VerbatimDisk(drive) => {
                    result.push(format!("{}:", drive.to_ascii_uppercase() as char))
                }
                Prefix::UNC(server, share) | Prefix::VerbatimUNC(server, share) => {
                    let mut prefix = OsString::from(r"\\");
                    prefix.push(server);
                    prefix.push(r"\");
                    prefix.push(share);
                    result.push(prefix);
                }
                _ => result.push(prefix.as_os_str()),
            },
            Component::RootDir => result.push(Path::new(r"\")),
            _ => result.push(component.as_os_str()),
        }
    }
    result
}
