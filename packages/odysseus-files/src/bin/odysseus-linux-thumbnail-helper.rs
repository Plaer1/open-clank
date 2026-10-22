//! Killable Linux thumbnail worker using the existing private OQL1 protocol.
#[path = "../quicklook_protocol.rs"]
#[allow(dead_code)]
mod wire;
#[cfg(target_os = "linux")]
#[path = "../linux_thumbnail.rs"]
mod provider;

#[cfg(target_os = "linux")]
fn run() -> Result<(), ()> {
    use std::io;
    if std::env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--probe")) {
        return if provider::probe() { Ok(()) } else { Err(()) };
    }
    let encoded = std::env::var("ODYSSEUS_QUICKLOOK_SESSION_TOKEN").map_err(|_| ())?;
    if encoded.len() != 64 || !encoded.is_ascii() { return Err(()); }
    let mut token = [0;32];
    for (i,byte) in token.iter_mut().enumerate() { *byte = u8::from_str_radix(&encoded[i*2..i*2+2],16).map_err(|_| ())?; }
    if std::env::var("ODYSSEUS_SHELL_IMAGE_KIND").as_deref() == Ok("icon") { return Err(()); }
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let respond = |out: &mut io::StdoutLock<'_>, response| {
        let bytes = wire::encode_response(&response).map_err(|_| ())?;
        wire::write_frame(out,&bytes,wire::MAX_HELPER_OUTPUT_BYTES+wire::RESPONSE_OVERHEAD_BYTES).map_err(|_| ())
    };
    respond(&mut output,wire::Response::Ready)?;
    loop {
        let bytes = wire::read_frame(&mut input,wire::MAX_COMMAND_FRAME_BYTES).map_err(|_| ())?;
        let command = wire::decode_command(&bytes).map_err(|_| ())?;
        let actual = match &command {
            wire::Command::Render(c) => &c.session_token,
            wire::Command::Cancel{session_token,..} | wire::Command::Shutdown{session_token} => session_token,
        };
        if !wire::token_matches(&token,actual) { return Err(()); }
        match command {
            wire::Command::Shutdown{..} => return Ok(()),
            wire::Command::Cancel{job_id,..} => respond(&mut output,wire::Response::Error{job_id,code:wire::HelperErrorCode::Cancelled})?,
            wire::Command::Render(request) => {
                let job_id = request.job_id;
                let response = match provider::render(request) {
                    Ok(bytes) => wire::Response::Png{job_id,bytes},
                    Err(code) => wire::Response::Error{job_id,code},
                };
                respond(&mut output,response)?;
            }
        }
    }
}
fn main() {
    #[cfg(target_os = "linux")]
    if run().is_ok() { return; }
    // Errors are deliberately path-blind and direct unauthenticated use is silent.
    std::process::exit(2);
}
