#[path = "../screencast.rs"]
mod screencast;
#[path = "../frame.rs"]
mod frame;

#[cfg(feature = "audio-foundation")]
#[path = "../audio_foundation.rs"]
mod audio_foundation;
#[cfg(feature = "audio-foundation")]
#[path = "../audio_registry.rs"]
mod audio_registry;
#[cfg(feature = "audio-foundation")]
#[path = "../audio_pcm.rs"]
mod audio_pcm;
#[cfg(feature = "audio-foundation")]
#[path = "../record_crop.rs"]
mod record_crop;
#[cfg(feature = "audio-foundation")]
#[path = "../crop_preview.rs"]
mod crop_preview;
#[path = "../observe.rs"]
mod observe;
#[path = "../record.rs"]
mod record;
#[cfg(feature = "audio-foundation")]
#[path = "../record_v6.rs"]
mod record_v6;
#[path = "../trim.rs"]
mod trim;

// Return a handle to this process itself over the private inherited socket.
// The parent never has to turn a possibly reused numeric PID into authority.
fn send_self_pidfd(raw:i32) -> bool {
    use std::os::fd::{AsRawFd,FromRawFd,OwnedFd};
    if raw<3{return false;}
    let socket=unsafe{OwnedFd::from_raw_fd(raw)};
    let mut stat=std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe{libc::fstat(socket.as_raw_fd(),stat.as_mut_ptr())}!=0 || (unsafe{stat.assume_init()}.st_mode & libc::S_IFMT)!=libc::S_IFSOCK{return false;}
    let pidfd=unsafe{libc::syscall(libc::SYS_pidfd_open,libc::getpid(),0)} as i32;
    if pidfd<0{return false;}let pidfd=unsafe{OwnedFd::from_raw_fd(pidfd)};
    let byte=[1u8];let mut iov=libc::iovec{iov_base:byte.as_ptr() as *mut _,iov_len:1};
    let space=unsafe{libc::CMSG_SPACE(std::mem::size_of::<libc::c_int>() as u32)} as usize;
    let mut control=vec![0usize;(space+std::mem::size_of::<usize>()-1)/std::mem::size_of::<usize>()];
    let mut msg:libc::msghdr=unsafe{std::mem::zeroed()};msg.msg_iov=&mut iov;msg.msg_iovlen=1;msg.msg_control=control.as_mut_ptr().cast();msg.msg_controllen=space;
    unsafe{let hdr=libc::CMSG_FIRSTHDR(&msg);if hdr.is_null(){return false;}(*hdr).cmsg_level=libc::SOL_SOCKET;(*hdr).cmsg_type=libc::SCM_RIGHTS;(*hdr).cmsg_len=libc::CMSG_LEN(std::mem::size_of::<libc::c_int>() as u32) as usize;std::ptr::write_unaligned(libc::CMSG_DATA(hdr).cast::<libc::c_int>(),pidfd.as_raw_fd());libc::sendmsg(socket.as_raw_fd(),&msg,libc::MSG_NOSIGNAL)==1}
}

// Called only by the explicit capability probe; no PipeWire core or device is opened.
#[cfg(feature = "audio-foundation")]
fn audio_runtime_available() -> bool {
    use gstreamer::prelude::*;
    if !unsafe { pipewire::sys::pw_check_library_version(1, 0, 5) } || gstreamer::init().is_err() { return false; }
    let names = ["pipewiresrc", "videoconvert", "videoscale", "videorate", "capsfilter", "vp8enc", "webmmux", "appsink", "appsrc", "queue", "audioconvert", "audioresample", "opusenc"];
    if names.iter().any(|name| gstreamer::ElementFactory::find(name).is_none()) { return false; }
    let Ok(appsrc) = gstreamer::ElementFactory::make("appsrc").build() else { return false; };
    appsrc.find_property("max-buffers").is_some() && appsrc.find_property("current-level-buffers").is_some()
}

#[cfg(feature = "audio-foundation")]
fn crop_runtime_available() -> bool {
    use gstreamer::prelude::*;
    let Ok(tee)=gstreamer::ElementFactory::make("tee").build() else{return false;};
    let Ok(valve)=gstreamer::ElementFactory::make("valve").build() else{return false;};
    let Ok(crop)=gstreamer::ElementFactory::make("videocrop").build() else{return false;};
    tee.pad_template("src_%u").is_some() && valve.find_property("drop").is_some()
        && ["left","right","top","bottom"].iter().all(|name|crop.find_property(name).is_some())
}

// The control profile is offered only when this PipeWire source can select a
// serial and refuse fallback or reconnect after that source disappears.
fn control_recording_runtime_available() -> bool {
    use gstreamer::prelude::*;
    if gstreamer::init().is_err() {return false;}
    let Ok(source)=gstreamer::ElementFactory::make("pipewiresrc").build() else{return false;};
    ["target-object","stream-properties","on-disconnect"].iter().all(|name|source.find_property(name).is_some())
}

// Private one-shot frame from a newly opened control-session remote.
fn control_frame(raw: i32) -> Result<(), ()> {
    use std::io::{Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    if raw < 3 { return Err(()); }
    let remote = unsafe { OwnedFd::from_raw_fd(raw) };
    let mut info=std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(remote.as_raw_fd(), info.as_mut_ptr()) } != 0 || (unsafe { info.assume_init() }.st_mode & libc::S_IFMT) != libc::S_IFSOCK { return Err(()); }
    #[derive(serde::Deserialize)] #[serde(deny_unknown_fields)]
    struct Request { version:u8, serial:u64 }
    let mut input=Vec::new(); std::io::stdin().take(1025).read_to_end(&mut input).map_err(|_|())?;
    if input.len()>1024 {return Err(());}
    let req:Request=serde_json::from_slice(&input).map_err(|_|())?;
    if req.version!=1 || req.serial==0 {return Err(());}
    let (loss, mut lost)=tokio::sync::mpsc::channel(1);
    let mut frames=frame::Frames::new_control(remote,req.serial,loss)?;
    for _ in 0..3 {
        if lost.try_recv().is_ok() {return Err(());}
        if let Some(frame)=frames.latest()? {
            if lost.try_recv().is_ok() {return Err(());}
            let header=serde_json::json!({"version":1,"state":"frame","pixel_size":[frame.width,frame.height],"png_bytes":frame.png.len()});
            let mut out=std::io::stdout().lock(); writeln!(out,"{header}").map_err(|_|())?;out.write_all(&frame.png).map_err(|_|())?;out.flush().map_err(|_|())?;return Ok(());
        }
    }
    Err(())
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let mut args = std::env::args().skip(1);
    let first = args.next();
    if first.as_deref() == Some("--capabilities") {
        if args.next().is_some() { std::process::exit(2); }
        let mut profiles = vec!["frame-v2", "record-v3", "trim-v4", "record-v5"];
        if control_recording_runtime_available() { profiles.push("record-v8"); profiles.push("control-frame-v1"); }
        #[cfg(feature = "audio-foundation")]
        if audio_runtime_available() { profiles.push("record-v6"); if crop_runtime_available() { profiles.push("record-v7"); } }
        println!("{}", serde_json::json!({"version":1,"profiles":profiles}));
        return;
    }
    if first.as_deref()==Some("--control-frame-v1") {
        if args.next().as_deref()!=Some("--remote-fd") {return;}
        let Some(remote)=args.next().and_then(|v|v.parse::<i32>().ok()) else{return;};
        if args.next().is_some() || control_frame(remote).is_err() {std::process::exit(1);} return;
    }
    #[cfg(feature = "audio-foundation")]
    if first.as_deref()==Some("--record-v6") {
        if args.next().as_deref()!=Some("--output-fd") {return;}
        let Some(fd)=args.next().and_then(|value|value.parse::<i32>().ok()) else{return;};
        if args.next().is_some(){return;}record_v6::run(fd,None).await;return;
    }
    #[cfg(feature = "audio-foundation")]
    if first.as_deref()==Some("--record-v7") {
        if args.next().as_deref()!=Some("--output-fd") {return;}let Some(fd)=args.next().and_then(|v|v.parse::<i32>().ok()) else{return;};
        if args.next().as_deref()!=Some("--preview-fd") {return;}let Some(preview)=args.next().and_then(|v|v.parse::<i32>().ok()) else{return;};
        if args.next().is_some(){return;}record_v6::run(fd,Some(preview)).await;return;
    }
    if first.as_deref()==Some("--record-v8") {
        if args.next().as_deref()!=Some("--output-fd") {return;}
        let Some(output)=args.next().and_then(|v|v.parse::<i32>().ok()) else{return;};
        if args.next().as_deref()!=Some("--remote-fd") {return;}
        let Some(remote)=args.next().and_then(|v|v.parse::<i32>().ok()) else{return;};
        if args.next().as_deref()!=Some("--serial") {return;}
        let Some(serial)=args.next().and_then(|v|v.parse::<u64>().ok()) else{return;};
        if args.next().as_deref()!=Some("--identity-fd") {return;}
        let Some(identity)=args.next().and_then(|v|v.parse::<i32>().ok()) else{return;};
        if args.next().is_some() || identity==output || identity==remote || !send_self_pidfd(identity) {return;}
        record::run_control(output,remote,serial).await;return;
    }
    if matches!(first.as_deref(), Some("--record-v3" | "--record-v5")) {
        if args.next().as_deref() != Some("--output-fd") { return; }
        let Some(fd) = args.next().and_then(|value| value.parse::<i32>().ok()) else { return; };
        if args.next().is_some() { return; }
        record::run(fd,if first.as_deref()==Some("--record-v5"){5}else{3}).await;
    } else if std::env::args().nth(1).as_deref() == Some("--trim-v4") {
        if args.next().as_deref() != Some("--input-fd") { return; }
        let Some(input) = args.next().and_then(|value| value.parse::<i32>().ok()) else { return; };
        if args.next().as_deref() != Some("--output-fd") { return; }
        let Some(output) = args.next().and_then(|value| value.parse::<i32>().ok()) else { return; };
        if args.next().is_some() { return; }
        trim::run(input,output).await;
    } else { screencast::run_with_frames(true).await; }
}
