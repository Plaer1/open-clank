//! Isolated Windows Shell extensions: same bounded authenticated broker protocol as QuickLook.
#[path = "../quicklook_protocol.rs"]
#[allow(dead_code)]
mod wire;
#[cfg(windows)]
mod native {
    use super::wire;
    use std::ffi::c_void;
    use std::fs::{File, OpenOptions};
    use std::io;
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    use std::path::Path;
    use std::ptr::null_mut;
    type Handle = *mut c_void;
    #[repr(C)]
    struct Guid {
        a: u32,
        b: u16,
        c: u16,
        d: [u8; 8],
    }
    #[repr(C)]
    struct Size {
        x: i32,
        y: i32,
    }
    #[repr(C)]
    struct Factory {
        vtable: *const FactoryVtable,
    }
    #[repr(C)]
    struct FactoryVtable {
        query: unsafe extern "system" fn(*mut Factory, *const Guid, *mut Handle) -> i32,
        add_ref: unsafe extern "system" fn(*mut Factory) -> u32,
        release: unsafe extern "system" fn(*mut Factory) -> u32,
        image: unsafe extern "system" fn(*mut Factory, Size, u32, *mut Handle) -> i32,
    }
    #[repr(C)]
    #[derive(Default)]
    struct Bitmap {
        kind: i32,
        width: i32,
        height: i32,
        width_bytes: i32,
        planes: u16,
        bits_pixel: u16,
        bits: Handle,
    }
    #[repr(C)]
    struct BitmapInfoHeader {
        size: u32,
        width: i32,
        height: i32,
        planes: u16,
        bits: u16,
        compression: u32,
        image_size: u32,
        xppm: i32,
        yppm: i32,
        used: u32,
        important: u32,
    }
    #[repr(C)]
    struct BitmapInfo {
        header: BitmapInfoHeader,
        colors: [u32; 3],
    }
    #[link(name = "ole32")]
    extern "system" {
        fn CoInitializeEx(reserved: Handle, mode: u32) -> i32;
        fn CoUninitialize();
    }
    #[link(name = "shell32")]
    extern "system" {
        fn SHCreateItemFromParsingName(
            path: *const u16,
            context: Handle,
            iid: *const Guid,
            output: *mut Handle,
        ) -> i32;
    }
    #[link(name = "gdi32")]
    extern "system" {
        fn GetObjectW(object: Handle, size: i32, output: Handle) -> i32;
        fn GetDIBits(
            dc: Handle,
            bitmap: Handle,
            start: u32,
            lines: u32,
            bits: Handle,
            info: *mut BitmapInfo,
            usage: u32,
        ) -> i32;
        fn CreateCompatibleDC(dc: Handle) -> Handle;
        fn DeleteDC(dc: Handle) -> i32;
        fn DeleteObject(object: Handle) -> i32;
    }
    struct Com;
    impl Drop for Com {
        fn drop(&mut self) {
            unsafe {
                CoUninitialize();
            }
        }
    }
    struct ShellFactory(*mut Factory);
    impl Drop for ShellFactory {
        fn drop(&mut self) {
            unsafe {
                ((*(*self.0).vtable).release)(self.0);
            }
        }
    }
    struct Image(Handle);
    impl Drop for Image {
        fn drop(&mut self) {
            unsafe {
                DeleteObject(self.0);
            }
        }
    }
    struct Dc(Handle);
    impl Drop for Dc {
        fn drop(&mut self) {
            unsafe {
                DeleteDC(self.0);
            }
        }
    }
    fn shell_spelling(path: &Path, file: &File) -> Result<String, wire::HelperErrorCode> {
        use wire::HelperErrorCode as Error;
        let text = path.to_str().ok_or(Error::InvalidRequest)?;
        let display = if let Some(tail) = text.strip_prefix(r"\\?\UNC\") {
            format!(r"\\{tail}")
        } else if let Some(tail) = text.strip_prefix(r"\\?\") {
            tail.to_owned()
        } else {
            return Err(Error::InvalidRequest);
        };
        let drive = display.as_bytes();
        let parts: Vec<_> = if display.starts_with(r"\\") {
            let parts: Vec<_> = display[2..].split('\\').collect();
            if parts.len() < 3 { return Err(Error::InvalidRequest); }
            parts
        } else if drive.len() >= 3 && drive[0].is_ascii_alphabetic()
            && drive[1] == b':' && drive[2] == b'\\' {
            display[3..].split('\\').collect()
        } else {
            return Err(Error::InvalidRequest);
        };
        for part in parts {
            let stem = part.split('.').next().unwrap_or_default().to_uppercase();
            let numbered_device = stem.strip_prefix("COM").or_else(|| stem.strip_prefix("LPT"))
                .is_some_and(|tail| matches!(tail, "1"|"2"|"3"|"4"|"5"|"6"|"7"|"8"|"9"|"¹"|"²"|"³"));
            if part.is_empty() || matches!(part, "." | "..") || part.ends_with([' ', '.'])
                || part.chars().any(|value| value < ' ' || "<>:\"/|?*".contains(value))
                || matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL") || numbered_device {
                return Err(Error::InvalidRequest);
            }
        }
        let alias = OpenOptions::new().read(true).share_mode(3).custom_flags(0x00200000)
            .open(&display).map_err(|_| Error::NotRegularFile)?;
        if std::fs::canonicalize(&display).map_err(|_| Error::NotRegularFile)? != path
            || alias.metadata().map_err(|_| Error::NotRegularFile)?.file_attributes() & 0x400 != 0
            || odysseus_files::windows_fs::opened_identity(&alias).map_err(|_| Error::NativeUnavailable)?
                != odysseus_files::windows_fs::opened_identity(file).map_err(|_| Error::NativeUnavailable)? {
            return Err(Error::NotRegularFile);
        }
        Ok(display)
    }
    fn render(command: wire::RenderCommand, icon: bool) -> Result<Vec<u8>, wire::HelperErrorCode> {
        use wire::HelperErrorCode as Error;
        if command.width == 0
            || command.height == 0
            || command.width > 1024
            || command.height > 1024
            || !command.scale.is_finite()
            || !(1.0..=3.0).contains(&command.scale)
            || command.max_output_bytes == 0
            || command.max_output_bytes as usize > wire::MAX_HELPER_OUTPUT_BYTES
        {
            return Err(Error::InvalidRequest);
        }
        let path_text = std::str::from_utf8(&command.path).map_err(|_| Error::InvalidRequest)?;
        if path_text.contains('\0') {
            return Err(Error::InvalidRequest);
        }
        let path = Path::new(path_text);
        let _parents =
            odysseus_files::windows_fs::pin_parent(path).map_err(|_| Error::NotRegularFile)?;
        let file = OpenOptions::new()
            .read(true)
            .share_mode(3)
            .custom_flags(0x00200000)
            .open(path)
            .map_err(|_| Error::NotRegularFile)?;
        if file
            .metadata()
            .map_err(|_| Error::NotRegularFile)?
            .file_attributes()
            & 0x400
            != 0
        {
            return Err(Error::NotRegularFile);
        }
        if !file
            .metadata()
            .map_err(|_| Error::NotRegularFile)?
            .is_file()
        {
            return Err(Error::NotRegularFile);
        }
        odysseus_files::windows_fs::validate_opened_path(&file, path)
            .map_err(|_| Error::NotRegularFile)?;
        let before = odysseus_files::windows_fs::opened_identity(&file)
            .map_err(|_| Error::NativeUnavailable)?;
        // Shell display parsing uses conventional DOS/UNC spelling; I/O authority
        // remains the pinned canonical path and the exact same opened file.
        let display = shell_spelling(path, &file)?;
        let pixels = unsafe {
            let mut factory = null_mut();
            let iid = Guid {
                a: 0xbcc18b79,
                b: 0xba16,
                c: 0x442f,
                d: [0x80, 0xc4, 0x8a, 0x59, 0xc3, 0x0c, 0x46, 0x3b],
            };
            let wide: Vec<u16> = display.encode_utf16().chain(Some(0)).collect();
            let parsed = SHCreateItemFromParsingName(wide.as_ptr(), null_mut(), &iid, &mut factory);
            if parsed < 0 {
                eprintln!("odysseus-shell-image phase=parse_item hresult={parsed}");
                return Err(Error::NativeUnavailable);
            }
            let factory = ShellFactory(factory as *mut Factory);
            let mut bitmap = null_mut();
            let size = Size {
                x: (command.width as f64 * command.scale).ceil() as i32,
                y: (command.height as f64 * command.scale).ceil() as i32,
            };
            // THUMBNAILONLY (0x8): never mislabel a generic Shell icon as content pixels.
            let rendered = ((*(*factory.0).vtable).image)(
                factory.0,
                size,
                if icon { 4 } else { 8 },
                &mut bitmap,
            );
            if rendered < 0 || bitmap.is_null()
            {
                eprintln!("odysseus-shell-image phase=get_image hresult={rendered}");
                return Err(Error::NativeUnavailable);
            }
            let bitmap = Image(bitmap);
            let mut description = Bitmap::default();
            if GetObjectW(
                bitmap.0,
                std::mem::size_of::<Bitmap>() as i32,
                &mut description as *mut _ as Handle,
            ) == 0
            {
                return Err(Error::EncodeFailed);
            }
            let width = description.width;
            let height = description
                .height
                .checked_abs()
                .ok_or(Error::OutputTooLarge)?;
            if width <= 0 || height <= 0 || width > 3072 || height > 3072 {
                return Err(Error::OutputTooLarge);
            }
            let dc = Dc(CreateCompatibleDC(null_mut()));
            if dc.0.is_null() {
                return Err(Error::EncodeFailed);
            }
            let mut info = BitmapInfo {
                header: BitmapInfoHeader {
                    size: 40,
                    width,
                    height: -height,
                    planes: 1,
                    bits: 32,
                    compression: 0,
                    image_size: 0,
                    xppm: 0,
                    yppm: 0,
                    used: 0,
                    important: 0,
                },
                colors: [0; 3],
            };
            let mut rgba = vec![0u8; width as usize * height as usize * 4];
            if GetDIBits(
                dc.0,
                bitmap.0,
                0,
                height as u32,
                rgba.as_mut_ptr() as Handle,
                &mut info,
                0,
            ) != height
            {
                return Err(Error::EncodeFailed);
            }
            let opaque = rgba.chunks_exact(4).all(|pixel| pixel[3] == 0);
            for pixel in rgba.chunks_exact_mut(4) {
                pixel.swap(0, 2);
                if opaque {
                    pixel[3] = 255;
                } else if pixel[3] > 0 && pixel[3] < 255 {
                    let alpha = pixel[3] as u32;
                    for color in &mut pixel[..3] {
                        *color = ((*color as u32 * 255 + alpha / 2) / alpha).min(255) as u8;
                    }
                }
            }
            let mut bytes = Vec::new();
            {
                let mut encoder = png::Encoder::new(&mut bytes, width as u32, height as u32);
                encoder.set_color(png::ColorType::Rgba);
                encoder.set_depth(png::BitDepth::Eight);
                let mut writer = encoder.write_header().map_err(|_| Error::EncodeFailed)?;
                writer
                    .write_image_data(&rgba)
                    .map_err(|_| Error::EncodeFailed)?;
            }
            bytes
        };
        odysseus_files::windows_fs::validate_opened_path(&file, path)
            .map_err(|_| Error::NotRegularFile)?;
        if before
            != odysseus_files::windows_fs::opened_identity(&file)
                .map_err(|_| Error::NativeUnavailable)?
        {
            return Err(Error::NotRegularFile);
        }
        if pixels.len() > command.max_output_bytes as usize {
            return Err(Error::OutputTooLarge);
        }
        Ok(pixels)
    }
    pub fn run() -> io::Result<()> {
        let encoded = std::env::var("ODYSSEUS_QUICKLOOK_SESSION_TOKEN").map_err(|_| {
            io::Error::new(
                io::ErrorKind::PermissionDenied,
                "private helper session missing",
            )
        })?;
        if encoded.len() != 64 || !encoded.is_ascii() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "invalid private helper session",
            ));
        }
        let mut token = [0u8; 32];
        for (index, value) in token.iter_mut().enumerate() {
            *value = u8::from_str_radix(&encoded[index * 2..index * 2 + 2], 16).map_err(|_| {
                io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "invalid private helper session",
                )
            })?;
        }
        if unsafe { CoInitializeEx(null_mut(), 2) } < 0 {
            return Err(io::Error::other("Shell COM initialization failed"));
        }
        let icon = match std::env::var("ODYSSEUS_SHELL_IMAGE_KIND").as_deref() {
            Ok("icon") => true,
            Ok("thumbnail") | Err(_) => false,
            _ => return Err(io::Error::other("invalid Shell image kind")),
        };
        let _com = Com;
        let mut input = io::stdin().lock();
        let mut output = io::stdout().lock();
        let respond = |output: &mut io::StdoutLock<'_>, response| -> io::Result<()> {
            let bytes = wire::encode_response(&response)
                .map_err(|error| io::Error::other(format!("{error:?}")))?;
            wire::write_frame(
                output,
                &bytes,
                wire::MAX_HELPER_OUTPUT_BYTES + wire::RESPONSE_OVERHEAD_BYTES,
            )
            .map_err(|error| io::Error::other(format!("{error:?}")))
        };
        respond(&mut output, wire::Response::Ready)?;
        // A hung extension is killed by the owning broker deadline; this process never blocks Files.
        loop {
            let bytes = match wire::read_frame(&mut input, wire::MAX_COMMAND_FRAME_BYTES) {
                Ok(bytes) => bytes,
                Err(error) => return Err(io::Error::other(format!("{error:?}"))),
            };
            let command = wire::decode_command(&bytes)
                .map_err(|error| io::Error::other(format!("{error:?}")))?;
            let actual = match &command {
                wire::Command::Render(command) => &command.session_token,
                wire::Command::Cancel { session_token, .. }
                | wire::Command::Shutdown { session_token } => session_token,
            };
            if !wire::token_matches(&token, actual) {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "private helper session mismatch",
                ));
            }
            match command {
                wire::Command::Shutdown { .. } => return Ok(()),
                wire::Command::Cancel { job_id, .. } => respond(
                    &mut output,
                    wire::Response::Error {
                        job_id,
                        code: wire::HelperErrorCode::Cancelled,
                    },
                )?,
                wire::Command::Render(command) => {
                    let job_id = command.job_id;
                    let response = match render(command, icon) {
                        Ok(bytes) => wire::Response::Png { job_id, bytes },
                        Err(code) => wire::Response::Error { job_id, code },
                    };
                    respond(&mut output, response)?;
                }
            }
        }
    }
}
#[cfg(windows)]
fn main() -> std::io::Result<()> {
    native::run()
}
#[cfg(not(windows))]
fn main() {
    eprintln!("Windows Shell thumbnail helper requires Windows");
    std::process::exit(1);
}
