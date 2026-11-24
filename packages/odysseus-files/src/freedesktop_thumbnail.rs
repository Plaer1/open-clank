//! XDG thumbnailers are host applications. Keep their configuration in the worker.
use super::{decode, file_uri, host_command, Error, MAX_HELPER_OUTPUT_BYTES};
use std::collections::{HashMap, HashSet};
use std::ffi::OsStr;
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

struct MimeGlob {
    pattern: String,
    mime: String,
    case_sensitive: bool,
    weight: u32,
}

struct Registry {
    programs: HashMap<String, Vec<String>>,
    globs: Vec<MimeGlob>,
}

fn registry() -> &'static Registry {
    static REGISTRY: OnceLock<Registry> = OnceLock::new();
    REGISTRY.get_or_init(|| Registry::load(&data_dirs()))
}

fn data_dirs() -> Vec<PathBuf> {
    let home = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| {
            std::env::var_os("HOME")
                .map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .map(|p| p.join(".local/share"))
        });
    let system = std::env::var_os("XDG_DATA_DIRS")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "/usr/local/share:/usr/share".into());
    home.into_iter()
        .chain(std::env::split_paths(&system).filter(|p| p.is_absolute()))
        .collect()
}

fn executable(program: &str) -> Option<PathBuf> {
    let path = Path::new(program);
    let runnable = |p: &Path| {
        p.metadata()
            .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    };
    if path.is_absolute() {
        return runnable(path).then(|| path.to_owned());
    }
    if program.is_empty() || program.contains('/') {
        return None;
    }
    std::env::split_paths(&std::env::var_os("PATH")?)
        .filter(|p| p.is_absolute())
        .map(|dir| dir.join(program))
        .find(|p| runnable(p))
}

fn entry(text: &str) -> Option<(Vec<String>, &str)> {
    let mut lines = text
        .lines()
        .map(str::trim)
        .skip_while(|l| *l != "[Thumbnailer Entry]");
    lines.next()?;
    let fields: HashMap<_, _> = lines
        .take_while(|l| !l.starts_with('['))
        .filter_map(|l| l.split_once('='))
        .collect();
    if let Some(program) = fields.get("TryExec") {
        executable(program)?;
    }
    let mut args = shlex::split(fields.get("Exec")?)?;
    let program = executable(args.first()?)?;
    args[0] = program.to_str()?.to_owned();
    Some((args, *fields.get("MimeType")?))
}

impl Registry {
    fn load(dirs: &[PathBuf]) -> Self {
        let mut programs = HashMap::new();
        let mut globs = Vec::new();
        let mut seen = HashSet::new();
        for dir in dirs {
            if let Ok(text) = fs::read_to_string(dir.join("mime/globs2")) {
                for line in text.lines() {
                    let mut fields = line.splitn(4, ':');
                    let Some(weight) = fields.next().and_then(|s| s.parse().ok()) else {
                        continue;
                    };
                    let (Some(mime), Some(pattern)) = (fields.next(), fields.next()) else {
                        continue;
                    };
                    let case_sensitive = fields
                        .next()
                        .is_some_and(|flags| flags.split(',').any(|f| f == "cs"));
                    let pattern = if case_sensitive {
                        pattern.to_owned()
                    } else {
                        pattern.to_lowercase()
                    };
                    globs.push(MimeGlob {
                        pattern,
                        mime: mime.into(),
                        case_sensitive,
                        weight,
                    });
                }
            }
            let Ok(files) = fs::read_dir(dir.join("thumbnailers")) else {
                continue;
            };
            let mut files: Vec<_> = files
                .flatten()
                .filter(|f| f.path().extension() == Some(OsStr::new("thumbnailer")))
                .collect();
            files.sort_by_key(|f| f.file_name());
            for file in files {
                if !seen.insert(file.file_name()) {
                    continue;
                }
                let Ok(text) = fs::read_to_string(file.path()) else {
                    continue;
                };
                let Some((args, types)) = entry(&text) else {
                    continue;
                };
                for mime in types.split(';').map(str::trim).filter(|m| !m.is_empty()) {
                    programs
                        .entry(mime.to_owned())
                        .or_insert_with(|| args.clone());
                }
            }
        }
        globs.sort_by_key(|g| std::cmp::Reverse((g.weight, g.pattern.len())));
        Self { programs, globs }
    }

    fn program(&self, name: &str) -> Option<&[String]> {
        let lower = name.to_lowercase();
        let mime = self.globs.iter().find(|g| {
            let name = if g.case_sensitive { name } else { &lower };
            match g.pattern.strip_prefix('*') {
                Some(suffix) => name.ends_with(suffix),
                None => name == g.pattern,
            }
        })?;
        self.programs.get(&mime.mime).map(Vec::as_slice)
    }
}
/// Expand %i %u %o %s %% anywhere inside each argument (pcmanfm does the same); other codes are refused.
fn expand(
    args: &[String],
    input: &Path,
    output: &Path,
    size: u32,
) -> Option<Vec<std::ffi::OsString>> {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    args.iter()
        .map(|arg| {
            let mut out = Vec::new();
            let mut bytes = arg.as_bytes().iter();
            while let Some(&b) = bytes.next() {
                if b != b'%' {
                    out.push(b);
                    continue;
                }
                match bytes.next() {
                    Some(b'i') => out.extend_from_slice(input.as_os_str().as_bytes()),
                    Some(b'u') => out.extend_from_slice(file_uri(input).as_bytes()),
                    Some(b'o') => out.extend_from_slice(output.as_os_str().as_bytes()),
                    Some(b's') => out.extend_from_slice(size.to_string().as_bytes()),
                    Some(b'%') => out.push(b'%'),
                    _ => return None,
                }
            }
            Some(std::ffi::OsString::from_vec(out))
        })
        .collect()
}

pub(super) fn available() -> bool {
    !registry().programs.is_empty()
}

pub(super) fn render(path: &Path, width: u32, height: u32) -> Result<Vec<u8>, Error> {
    let name = path
        .file_name()
        .ok_or(Error::NativeUnavailable)?
        .to_string_lossy();
    let program = registry().program(&name).ok_or(Error::NativeUnavailable)?;
    let dir = tempfile::Builder::new()
        .permissions(fs::Permissions::from_mode(0o700))
        .tempdir()
        .map_err(|_| Error::NativeUnavailable)?;
    let output = dir.path().join("preview.png");
    let size = width.max(height).next_power_of_two().clamp(128, 1024);
    let args = expand(&program[1..], path, &output, size).ok_or(Error::NativeUnavailable)?;
    let mut child = host_command(Path::new(&program[0]))
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| Error::NativeUnavailable)?;
    let deadline = Instant::now() + Duration::from_millis(1800);
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(Error::NativeUnavailable);
            }
        }
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(output)
        .map_err(|_| Error::NativeUnavailable)?;
    let meta = file.metadata().map_err(|_| Error::NativeUnavailable)?;
    if !meta.is_file() {
        return Err(Error::NativeUnavailable);
    }
    if meta.len() > MAX_HELPER_OUTPUT_BYTES as u64 {
        return Err(Error::OutputTooLarge);
    }
    let mut bytes = Vec::new();
    file.take(MAX_HELPER_OUTPUT_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::NativeUnavailable)?;
    decode(&bytes, width, height, None)
}
