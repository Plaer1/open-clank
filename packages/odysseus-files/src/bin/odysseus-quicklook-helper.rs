#[cfg(target_os = "macos")]
#[allow(dead_code)]
#[path = "../quicklook_protocol.rs"]
mod quicklook_protocol;

#[cfg(target_os = "macos")]
mod macos {
    use super::quicklook_protocol as wire;
    use block2::RcBlock;
    use objc2::rc::{autoreleasepool, Retained};
    use objc2::AnyThread;
    use objc2_core_foundation::{CFMutableData, CFString, CGSize};
    use objc2_foundation::{NSDate, NSError, NSNumber, NSRunLoop, NSURLIsPackageKey, NSURL};
    use objc2_image_io::CGImageDestination;
    use objc2_quick_look_thumbnailing::{
        QLThumbnailGenerationRequest, QLThumbnailGenerationRequestRepresentationTypes,
        QLThumbnailGenerator, QLThumbnailRepresentation, QLThumbnailRepresentationType,
    };
    use std::env;
    use std::ffi::OsString;
    use std::fs;
    use std::io;
    use std::os::unix::ffi::OsStringExt;
    use std::path::{Path, PathBuf};
    use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError};
    use std::thread;

    const SESSION_ENV: &str = "ODYSSEUS_QUICKLOOK_SESSION_TOKEN";
    const ACTOR_QUEUE_CAPACITY: usize = 8;
    const OUTPUT_QUEUE_CAPACITY: usize = 2;
    const MAX_DIMENSION: u32 = 1024;
    const MAX_SCALE: f64 = 3.0;

    pub fn run() -> io::Result<()> {
        let token = env::var(SESSION_ENV)
            .ok()
            .and_then(|value| decode_token(&value))
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::PermissionDenied, "private channel required")
            })?;

        let (command_sender, command_receiver) = mpsc::sync_channel(ACTOR_QUEUE_CAPACITY);
        let (response_sender, response_receiver) = mpsc::sync_channel(OUTPUT_QUEUE_CAPACITY);
        let _reader = spawn_reader(command_sender, response_sender.clone())?;
        let _writer = spawn_writer(response_receiver)?;

        response_sender
            .send(wire::Response::Ready)
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "private channel closed"))?;
        run_actor(token, command_receiver, response_sender);
        Ok(())
    }

    fn spawn_reader(
        sender: SyncSender<wire::Command>,
        responses: SyncSender<wire::Response>,
    ) -> io::Result<thread::JoinHandle<()>> {
        thread::Builder::new()
            .name("odysseus-quicklook-command-reader".into())
            .spawn(move || {
                let stdin = io::stdin();
                let mut input = stdin.lock();
                loop {
                    let command = wire::read_frame(&mut input, wire::MAX_COMMAND_FRAME_BYTES)
                        .and_then(|payload| wire::decode_command(&payload));
                    match command {
                        Ok(command) => {
                            if sender.send(command).is_err() {
                                return;
                            }
                        }
                        Err(_) => {
                            let _ = responses.send(wire::Response::Error {
                                job_id: 0,
                                code: wire::HelperErrorCode::Malformed,
                            });
                            return;
                        }
                    }
                }
            })
    }

    fn spawn_writer(receiver: Receiver<wire::Response>) -> io::Result<thread::JoinHandle<()>> {
        thread::Builder::new()
            .name("odysseus-quicklook-response-writer".into())
            .spawn(move || {
                let stdout = io::stdout();
                let mut output = stdout.lock();
                while let Ok(response) = receiver.recv() {
                    let payload = match wire::encode_response(&response) {
                        Ok(payload) => payload,
                        Err(_) => return,
                    };
                    if wire::write_frame(
                        &mut output,
                        &payload,
                        wire::MAX_HELPER_OUTPUT_BYTES + wire::RESPONSE_OVERHEAD_BYTES,
                    )
                    .is_err()
                    {
                        return;
                    }
                }
            })
    }

    fn run_actor(
        session_token: [u8; wire::SESSION_TOKEN_BYTES],
        commands: Receiver<wire::Command>,
        responses: SyncSender<wire::Response>,
    ) {
        autoreleasepool(|_| {
            let generator = unsafe { QLThumbnailGenerator::sharedGenerator() };
            let run_loop = NSRunLoop::currentRunLoop();
            let (native_sender, native_receiver) = mpsc::channel();
            let mut active: Option<ActiveJob> = None;
            let mut running = true;

            while running {
                loop {
                    match commands.try_recv() {
                        Ok(command) => {
                            if !command_has_token(&command, &session_token) {
                                let job_id = command_job_id(&command);
                                let _ = responses.send(wire::Response::Error {
                                    job_id,
                                    code: wire::HelperErrorCode::UnauthorizedChannel,
                                });
                                running = false;
                                break;
                            }
                            match command {
                                wire::Command::Render(command) => {
                                    if active.is_some() {
                                        let _ = responses.send(wire::Response::Error {
                                            job_id: command.job_id,
                                            code: wire::HelperErrorCode::Busy,
                                        });
                                        continue;
                                    }
                                    match start_job(&generator, command, native_sender.clone()) {
                                        Ok(job) => active = Some(job),
                                        Err((job_id, code)) => {
                                            let _ = responses
                                                .send(wire::Response::Error { job_id, code });
                                        }
                                    }
                                }
                                wire::Command::Cancel { job_id, .. } => {
                                    if active.as_ref().is_some_and(|job| job.job_id == job_id) {
                                        let job = active.take().expect("active job checked");
                                        unsafe { generator.cancelRequest(&job.request) };
                                        let _ = responses.send(wire::Response::Error {
                                            job_id,
                                            code: wire::HelperErrorCode::Cancelled,
                                        });
                                    }
                                }
                                wire::Command::Shutdown { .. } => {
                                    if let Some(job) = active.take() {
                                        unsafe { generator.cancelRequest(&job.request) };
                                    }
                                    running = false;
                                    break;
                                }
                            }
                        }
                        Err(TryRecvError::Empty) => break,
                        Err(TryRecvError::Disconnected) => {
                            running = false;
                            break;
                        }
                    }
                }

                while let Ok(completion) = native_receiver.try_recv() {
                    if active
                        .as_ref()
                        .is_some_and(|job| job.job_id == completion.job_id)
                    {
                        active = None;
                        let response = match completion.result {
                            Ok(bytes) => wire::Response::Png {
                                job_id: completion.job_id,
                                bytes,
                            },
                            Err(code) => wire::Response::Error {
                                job_id: completion.job_id,
                                code,
                            },
                        };
                        let _ = responses.send(response);
                    }
                }

                if running {
                    autoreleasepool(|_| {
                        let until = NSDate::dateWithTimeIntervalSinceNow(0.01);
                        run_loop.runUntilDate(&until);
                    });
                }
            }
        });
    }

    struct ActiveJob {
        job_id: u64,
        request: Retained<QLThumbnailGenerationRequest>,
        _completion: RcBlock<dyn Fn(*mut QLThumbnailRepresentation, *mut NSError)>,
    }

    struct NativeCompletion {
        job_id: u64,
        result: Result<Vec<u8>, wire::HelperErrorCode>,
    }

    fn start_job(
        generator: &QLThumbnailGenerator,
        command: wire::RenderCommand,
        sender: mpsc::Sender<NativeCompletion>,
    ) -> Result<ActiveJob, (u64, wire::HelperErrorCode)> {
        let job_id = command.job_id;
        if command.width == 0
            || command.height == 0
            || command.width > MAX_DIMENSION
            || command.height > MAX_DIMENSION
            || !command.scale.is_finite()
            || command.scale < 1.0
            || command.scale > MAX_SCALE
            || command.max_output_bytes == 0
            || command.max_output_bytes as usize > wire::MAX_HELPER_OUTPUT_BYTES
        {
            return Err((job_id, wire::HelperErrorCode::InvalidRequest));
        }
        let path = PathBuf::from(OsString::from_vec(command.path));
        if !is_canonical_regular_file(&path) {
            return Err((job_id, wire::HelperErrorCode::NotRegularFile));
        }
        let url =
            NSURL::from_file_path(&path).ok_or((job_id, wire::HelperErrorCode::InvalidRequest))?;
        if is_package(&url).unwrap_or(true) {
            return Err((job_id, wire::HelperErrorCode::NotRegularFile));
        }
        let request = unsafe {
            QLThumbnailGenerationRequest::initWithFileAtURL_size_scale_representationTypes(
                QLThumbnailGenerationRequest::alloc(),
                &url,
                CGSize::new(command.width as f64, command.height as f64),
                command.scale,
                QLThumbnailGenerationRequestRepresentationTypes::All,
            )
        };
        unsafe { request.setIconMode(false) };

        let max_output_bytes = command.max_output_bytes as usize;
        let completion: RcBlock<dyn Fn(*mut QLThumbnailRepresentation, *mut NSError)> =
            RcBlock::new(
                move |representation: *mut QLThumbnailRepresentation, _error: *mut NSError| {
                    let result = autoreleasepool(|_| {
                        encode_native_representation(representation, max_output_bytes)
                    });
                    let _ = sender.send(NativeCompletion { job_id, result });
                },
            );
        unsafe {
            generator.generateBestRepresentationForRequest_completionHandler(&request, &completion)
        };
        Ok(ActiveJob {
            job_id,
            request,
            _completion: completion,
        })
    }

    fn encode_native_representation(
        representation: *mut QLThumbnailRepresentation,
        max_output_bytes: usize,
    ) -> Result<Vec<u8>, wire::HelperErrorCode> {
        let representation =
            unsafe { representation.as_ref() }.ok_or(wire::HelperErrorCode::NativeUnavailable)?;
        let representation_type = unsafe { representation.r#type() };
        // Quick Look chooses the best available representation for an All request:
        // content thumbnails take priority, with the native file icon as fallback.
        if representation_type != QLThumbnailRepresentationType::Thumbnail
            && representation_type != QLThumbnailRepresentationType::LowQualityThumbnail
            && representation_type != QLThumbnailRepresentationType::Icon
        {
            return Err(wire::HelperErrorCode::NativeUnavailable);
        }
        let image = unsafe { representation.CGImage() };
        let data = CFMutableData::new(None, 0).ok_or(wire::HelperErrorCode::EncodeFailed)?;
        let png_type = CFString::from_static_str("public.png");
        let destination = unsafe { CGImageDestination::with_data(&data, &png_type, 1, None) }
            .ok_or(wire::HelperErrorCode::EncodeFailed)?;
        unsafe { destination.add_image(&image, None) };
        if !unsafe { destination.finalize() } {
            return Err(wire::HelperErrorCode::EncodeFailed);
        }
        if data.len() > max_output_bytes {
            return Err(wire::HelperErrorCode::OutputTooLarge);
        }
        Ok(data.to_vec())
    }

    fn is_canonical_regular_file(path: &Path) -> bool {
        if !path.is_absolute() || fs::canonicalize(path).ok().as_deref() != Some(path) {
            return false;
        }
        fs::symlink_metadata(path)
            .map(|metadata| metadata.file_type().is_file())
            .unwrap_or(false)
    }

    fn is_package(url: &NSURL) -> Result<bool, ()> {
        let mut value = None;
        unsafe { url.getResourceValue_forKey_error(&mut value, NSURLIsPackageKey) }
            .map_err(|_| ())?;
        let value = value.ok_or(())?;
        let number = value.downcast_ref::<NSNumber>().ok_or(())?;
        Ok(number.boolValue())
    }

    fn command_has_token(
        command: &wire::Command,
        expected: &[u8; wire::SESSION_TOKEN_BYTES],
    ) -> bool {
        let actual = match command {
            wire::Command::Render(command) => &command.session_token,
            wire::Command::Cancel { session_token, .. }
            | wire::Command::Shutdown { session_token } => session_token,
        };
        wire::token_matches(expected, actual)
    }

    fn command_job_id(command: &wire::Command) -> u64 {
        match command {
            wire::Command::Render(command) => command.job_id,
            wire::Command::Cancel { job_id, .. } => *job_id,
            wire::Command::Shutdown { .. } => 0,
        }
    }

    fn decode_token(encoded: &str) -> Option<[u8; wire::SESSION_TOKEN_BYTES]> {
        if encoded.len() != wire::SESSION_TOKEN_BYTES * 2 {
            return None;
        }
        let mut token = [0_u8; wire::SESSION_TOKEN_BYTES];
        for (index, chunk) in encoded.as_bytes().chunks_exact(2).enumerate() {
            token[index] = decode_nibble(chunk[0])? << 4 | decode_nibble(chunk[1])?;
        }
        Some(token)
    }

    fn decode_nibble(value: u8) -> Option<u8> {
        match value {
            b'0'..=b'9' => Some(value - b'0'),
            b'a'..=b'f' => Some(value - b'a' + 10),
            b'A'..=b'F' => Some(value - b'A' + 10),
            _ => None,
        }
    }
}

#[cfg(target_os = "macos")]
fn main() {
    if macos::run().is_err() {
        std::process::exit(2);
    }
}

#[cfg(not(target_os = "macos"))]
fn main() {
    // Non-macOS builds intentionally contain no native thumbnail adapter.
    std::process::exit(2);
}
