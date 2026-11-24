//! Internal finite VP8/WebM forward-decode trim; no portal, URI or source overwrite.
use std::{fs::File, io::{self, Read, Seek, SeekFrom, Write}, os::fd::FromRawFd, sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}}, time::{Duration,SystemTime,UNIX_EPOCH}};
use gstreamer as gst;
use gstreamer_app as gst_app;
use gst::prelude::*;
use serde::Deserialize;
use tokio::{io::{AsyncBufReadExt,AsyncReadExt,BufReader},sync::mpsc};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {version:u8,action:String,generation:String,deadline_ms:u64,start_ms:u64,end_ms:u64,max_bytes:u64}
fn now()->u64{SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis().min(u64::MAX as u128) as u64}
fn send(value:serde_json::Value){let mut out=io::stdout().lock();let _=serde_json::to_writer(&mut out,&value);let _=out.write_all(b"\n");let _=out.flush();}
fn owned_fd(fd:i32,write:bool)->Result<File,()>{
 if fd<3{return Err(());}let flags=unsafe{libc::fcntl(fd,libc::F_GETFL)};
 if flags<0 || (flags&libc::O_ACCMODE)!=if write{libc::O_WRONLY}else{libc::O_RDONLY}{return Err(());}
 let mut info=std::mem::MaybeUninit::<libc::stat>::uninit();if unsafe{libc::fstat(fd,info.as_mut_ptr())}!=0{return Err(());}let info=unsafe{info.assume_init()};
 if write {if (info.st_mode&libc::S_IFMT)!=libc::S_IFIFO{return Err(());}} else if (info.st_mode&libc::S_IFMT)!=libc::S_IFREG||info.st_size<=0||info.st_size>67108864{return Err(());}
 Ok(unsafe{File::from_raw_fd(fd)})
}
fn vint(bytes:&[u8],at:&mut usize,id:bool)->Option<u64>{let first=*bytes.get(*at)?;let n=first.leading_zeros() as usize+1;if n>8||(*at).checked_add(n)?>bytes.len(){return None;}let mut value=if id{first as u64}else{(first & ((0xffu16>>n) as u8)) as u64};for byte in &bytes[*at+1..*at+n]{value=(value<<8)|u64::from(*byte);}*at+=n;Some(value)}
fn webm(file:&mut File)->bool{
 let mut buf=vec![0u8;4096];let Ok(n)=file.read(&mut buf) else{return false;};buf.truncate(n);if file.seek(SeekFrom::Start(0)).is_err(){return false;}if buf.get(..4)!=Some(&[0x1a,0x45,0xdf,0xa3]){return false;}let mut pos=4;let Some(size)=vint(&buf,&mut pos,false).and_then(|n|usize::try_from(n).ok()) else{return false;};let Some(end)=pos.checked_add(size).filter(|end|*end<=buf.len()) else{return false;};let mut found=false;while pos<end{let Some(id)=vint(&buf,&mut pos,true) else{return false;};let Some(size)=vint(&buf,&mut pos,false).and_then(|n|usize::try_from(n).ok()) else{return false;};let Some(next)=pos.checked_add(size).filter(|next|*next<=end) else{return false;};if id==0x4282{if found||!matches!(buf.get(pos..next),Some(b"webm")|Some(b"webm\0")){return false;}found=true;}pos=next;}found
}
#[derive(Default)]
struct State {tracks:u32,ready:bool,bad:bool,segment:Option<gst::FormattedSegment<gst::ClockTime>>,first:Option<u64>,last:Option<u64>,seen:Option<u64>,last_duration:Option<u64>,selected:u64,reached_end:bool}
struct Pipeline {pipe:gst::Pipeline,sink:gst_app::AppSink,state:Arc<Mutex<State>>,encoded:Arc<std::sync::atomic::AtomicU64>}
impl Drop for Pipeline{fn drop(&mut self){let _=self.pipe.set_state(gst::State::Null);}}
fn pipeline(input:&File,req:&Request,events:mpsc::Sender<&'static str>)->Result<Pipeline,()>{
 use std::os::fd::AsRawFd;
 gst::init().map_err(|_|())?;let pipe=gst::Pipeline::default();let bus=pipe.bus().ok_or(())?;
 bus.set_sync_handler(move|_,msg|{let v=match msg.view(){gst::MessageView::Error(_)=>Some("error"),gst::MessageView::Eos(_)=>Some("eos"),_=>None};if let Some(v)=v{let _=events.try_send(v);}gst::BusSyncReply::Drop});
 let source=gst::ElementFactory::make("fdsrc").property("fd",input.as_raw_fd()).build().map_err(|_|())?;
 let demux=gst::ElementFactory::make("matroskademux").build().map_err(|_|())?;
 let decoder=gst::ElementFactory::make("vp8dec").build().map_err(|_|())?;
 let convert=gst::ElementFactory::make("videoconvert").build().map_err(|_|())?;
 let caps=gst::ElementFactory::make("capsfilter").property("caps",gst::Caps::builder("video/x-raw").field("format","I420").field("width",gst::IntRange::new(1i32,1920i32)).field("height",gst::IntRange::new(1i32,1080i32)).field("framerate",gst::FractionRange::new(gst::Fraction::new(0,1),gst::Fraction::new(30,1))).build()).build().map_err(|_|())?;
 let encoder=gst::ElementFactory::make("vp8enc").build().map_err(|_|())?;let mux=gst::ElementFactory::make("webmmux").property("streamable",true).build().map_err(|_|())?;
 let sink=gst_app::AppSink::builder().build();sink.set_max_buffers(1);sink.set_drop(false);sink.set_wait_on_eos(false);
 pipe.add_many([&source,&demux,&decoder,&convert,&caps,&encoder,&mux,sink.upcast_ref()]).map_err(|_|())?;source.link(&demux).map_err(|_|())?;gst::Element::link_many([&decoder,&convert,&caps,&encoder,&mux,sink.upcast_ref()]).map_err(|_|())?;
 let state=Arc::new(Mutex::new(State::default()));
 {let state=state.clone();let decoder=decoder.clone();demux.connect_pad_added(move|_,pad|{let Ok(mut st)=state.lock() else{return;};st.tracks+=1;let valid=pad.current_caps().and_then(|v|v.structure(0).map(|s|s.name()=="video/x-vp8")).unwrap_or(false);if st.tracks!=1||!valid{st.bad=true;return;}if decoder.static_pad("sink").is_none_or(|target|pad.link(&target).is_err()){st.bad=true;}});}
 {let state=state.clone();demux.connect_no_more_pads(move|_|{if let Ok(mut st)=state.lock(){st.ready=st.tracks==1&&!st.bad;}});}
 let start=req.start_ms.checked_mul(1_000_000).ok_or(())?;let end=req.end_ms.checked_mul(1_000_000).ok_or(())?;
 {let state=state.clone();decoder.static_pad("src").ok_or(())?.add_probe(gst::PadProbeType::BUFFER|gst::PadProbeType::EVENT_DOWNSTREAM,move|_,probe|{
   let Ok(mut st)=state.lock() else{return gst::PadProbeReturn::Drop;};
   if let Some(event)=probe.event(){if let gst::EventView::Segment(segment)=event.view(){if st.segment.is_some(){st.bad=true;return gst::PadProbeReturn::Drop;}let Some(seg)=segment.segment().downcast_ref::<gst::ClockTime>() else{st.bad=true;return gst::PadProbeReturn::Drop;};if seg.rate()!=1.0||seg.applied_rate()!=1.0{st.bad=true;return gst::PadProbeReturn::Drop;}st.segment=Some(seg.clone());if let Some(event)=probe.event_mut(){*event=gst::event::Segment::new(&gst::FormattedSegment::<gst::ClockTime>::new());}}return gst::PadProbeReturn::Ok;}
   let Some(buffer)=probe.buffer_mut() else{return gst::PadProbeReturn::Drop;};if !st.ready||st.bad{st.bad=true;return gst::PadProbeReturn::Drop;}
   let Some(pts)=buffer.pts() else{st.bad=true;return gst::PadProbeReturn::Drop;};let Some(time)=st.segment.as_ref().and_then(|s|s.to_stream_time(pts)) else{st.bad=true;return gst::PadProbeReturn::Drop;};let n=time.nseconds();
   if st.seen.is_some_and(|last|n<=last) || (st.seen.is_some() && buffer.flags().contains(gst::BufferFlags::DISCONT)){st.bad=true;return gst::PadProbeReturn::Drop;}st.seen=Some(n);if n<start{return gst::PadProbeReturn::Drop;}if n>=end{st.reached_end=true;return gst::PadProbeReturn::Drop;}
   let first=*st.first.get_or_insert(n);st.last=Some(n);st.last_duration=buffer.duration().map(|v|v.nseconds());st.selected+=1;let b=buffer.make_mut();b.set_pts(gst::ClockTime::from_nseconds(n-first));b.set_dts(None);gst::PadProbeReturn::Ok
  }).ok_or(())?;}
 let encoded=Arc::new(std::sync::atomic::AtomicU64::new(0));{let count=encoded.clone();encoder.static_pad("src").ok_or(())?.add_probe(gst::PadProbeType::BUFFER,move|_,_|{count.fetch_add(1,Ordering::Relaxed);gst::PadProbeReturn::Ok}).ok_or(())?;}
 if pipe.set_state(gst::State::Playing).is_err(){let _=pipe.set_state(gst::State::Null);return Err(());}Ok(Pipeline{pipe,sink,state,encoded})
}

struct Abort(Arc<AtomicBool>);
impl Drop for Abort{fn drop(&mut self){self.0.store(true,Ordering::Release);}}
pub async fn run(input_fd:i32,output_fd:i32){
 if input_fd==output_fd{return;}let Ok(mut input)=owned_fd(input_fd,false) else{return;};let Ok(output)=owned_fd(output_fd,true) else{return;};if !webm(&mut input){return;}
 let(tx,mut rx)=mpsc::channel::<Option<Vec<u8>>>(1);tokio::spawn(async move{let mut input=BufReader::new(tokio::io::stdin());loop{let mut line=Vec::new();let read=(&mut input).take(8194).read_until(b'\n',&mut line).await;if !matches!(read,Ok(n) if n>0)||line.len()>8192||!line.ends_with(b"\n"){let _=tx.send(None).await;break;}if tx.send(Some(line)).await.is_err(){break;}}});
 let Some(Some(line))=rx.recv().await else{return;};let Ok(req)=serde_json::from_slice::<Request>(&line)else{return;};
 if req.version!=4||req.action!="trim"||req.generation.len()!=32||!req.generation.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))||req.start_ms>=req.end_ms||req.end_ms>86400000||req.end_ms-req.start_ms>60000||req.deadline_ms<=now()||req.deadline_ms>now().saturating_add(120000)||req.max_bytes==0||req.max_bytes>67108864{return;}
 let deadline=tokio::time::sleep(Duration::from_millis(req.deadline_ms.saturating_sub(now())));tokio::pin!(deadline);
 let(events_tx,mut events)=mpsc::channel(1);let Ok(pipeline)=pipeline(&input,&req,events_tx) else{return;};
 let abort=Arc::new(AtomicBool::new(false));let _guard=Abort(abort.clone());let finishing=Arc::new(AtomicBool::new(false));
 let worker=tokio::task::spawn_blocking({let sink=pipeline.sink.clone();let abort=abort.clone();let finishing=finishing.clone();move||crate::record::write_output(sink,output,req.max_bytes,abort,finishing)});tokio::pin!(worker);
 send(serde_json::json!({"version":4,"state":"starting","generation":req.generation}));
 let eos=tokio::select!{biased;_=&mut deadline=>false,_=rx.recv()=>false,event=events.recv()=>event==Some("eos"),result=&mut worker=>{let _=result;return;}};
 if eos{finishing.store(true,Ordering::Release);}else{abort.store(true,Ordering::Release);let _=pipeline.pipe.set_state(gst::State::Null);}
 let Ok(Ok(output))=tokio::time::timeout(Duration::from_secs(2),&mut worker).await else{return;};
 let Ok(st)=pipeline.state.lock() else{return;};let encoded=pipeline.encoded.load(Ordering::Relaxed);
 let valid=eos&&!output.failed&&!output.limit&&!st.bad&&st.ready&&st.selected>0&&encoded==st.selected&&output.bytes>0;
 let coverage=if st.reached_end||st.last.zip(st.last_duration).and_then(|(x,y)|x.checked_add(y)).is_some_and(|v|v>=req.end_ms*1_000_000){"reached_end"}else if st.last_duration.is_some(){"source_eos_before_end"}else{"unknown"};
 send(serde_json::json!({"version":4,"state":if valid{"finalized"}else{"partial"},"generation":req.generation,"reason":if valid{"eos"}else if output.limit{"output_limit"}else if !eos{"cancelled"}else{"unsupported"},"requested_start_ms":req.start_ms,"requested_end_ms":req.end_ms,"first_pts_ns":st.first,"last_pts_ns":st.last,"last_duration_ns":st.last_duration,"timebase":"source_stream_nanoseconds","selected_frames":st.selected,"encoded_frames":encoded,"coverage":coverage,"bytes_written":output.bytes}));
 drop(st);drop(pipeline);drop(input);
}
