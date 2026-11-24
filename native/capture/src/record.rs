//! Internal human-owned ScreenCast recording; all bytes leave through a passed pipe.
use std::{fs::File, io::{self, Write}, os::fd::{FromRawFd, OwnedFd}, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::{Duration, SystemTime, UNIX_EPOCH}};
use ashpd::desktop::{PersistMode, screencast::{CursorMode, Screencast, SelectSourcesOptions, SourceType}};
use futures_util::StreamExt;
use gstreamer as gst;
use gstreamer_app as gst_app;
use gst::prelude::*;
use serde::Deserialize;
use tokio::{io::{AsyncBufReadExt, AsyncReadExt, BufReader}, sync::mpsc};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Start { pub(crate) version:u8, pub(crate) action:String, pub(crate) generation:String, pub(crate) deadline_ms:u64, pub(crate) duration_ms:u64, pub(crate) fps:u32, pub(crate) max_width:u32, pub(crate) max_height:u32, pub(crate) max_bytes:u64 }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command { version:u8, action:String, generation:String }
pub(crate) fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis().min(u64::MAX as u128) as u64 }
pub(crate) fn send(value:serde_json::Value) { let mut out=io::stdout().lock();let _=serde_json::to_writer(&mut out,&value);let _=out.write_all(b"\n");let _=out.flush(); }

fn observe_encoded(encoder:&gst::Element, first:mpsc::Sender<()>, seen:Arc<AtomicBool>) -> Result<(),()> {
 let pad=encoder.static_pad("src").ok_or(())?;
 pad.add_probe(gst::PadProbeType::BUFFER, move |_,_| {
  if !seen.swap(true,Ordering::AcqRel) { let _=first.try_send(()); }
  gst::PadProbeReturn::Ok
 }).ok_or(())?;
 Ok(())
}
pub(crate) struct Pipeline { pub(crate) pipe:gst::Pipeline, pub(crate) sink:gst_app::AppSink, pub(crate) mux:gst::Element, caps:gst::Element, rate:gst::Element, observer:Option<crate::observe::Observer>, #[cfg(feature="audio-foundation")] pub(crate) crop:Option<crate::crop_preview::CropPreview> }
impl Pipeline {
 pub(crate) fn new(remote:&OwnedFd,node:u32,req:&Start,events:mpsc::Sender<&'static str>,first:mpsc::Sender<()>,seen:Arc<AtomicBool>,play:bool) -> Result<Self,()> {
  use std::os::fd::AsRawFd;
  gst::init().map_err(|_|())?;
  let source=gst::ElementFactory::make("pipewiresrc").property("fd",remote.as_raw_fd()).property("path",node.to_string()).build().map_err(|_|())?;
  if source.find_property("on-disconnect").is_none() {return Err(());} source.set_property_from_str("on-disconnect","error");
  Self::with_source(source,req,events,first,seen,play)
 }
 pub(crate) fn new_control(remote:&OwnedFd,serial:u64,req:&Start,events:mpsc::Sender<&'static str>,first:mpsc::Sender<()>,seen:Arc<AtomicBool>) -> Result<Self,()> {
  use std::os::fd::AsRawFd;
  if serial==0{return Err(());} gst::init().map_err(|_|())?;
  let source=gst::ElementFactory::make("pipewiresrc").property("fd",remote.as_raw_fd()).build().map_err(|_|())?;
  if ["target-object","stream-properties","on-disconnect"].iter().any(|key|source.find_property(key).is_none()){return Err(());}
  source.set_property("target-object",serial.to_string());source.set_property_from_str("on-disconnect","error");
  let props=gst::Structure::builder("props").field("node.dont-reconnect","true").field("node.dont-fallback","true").field("node.dont-move","true").build();
  source.set_property("stream-properties",props);
  Self::with_source(source,req,events,first,seen,true)
 }
 /// Uses the same recorder path with an already-owned source; synthetic qualification injects a test source here.
 pub(crate) fn with_source(source:gst::Element,req:&Start,events:mpsc::Sender<&'static str>,first:mpsc::Sender<()>,seen:Arc<AtomicBool>,play:bool)->Result<Self,()> {
  let convert=gst::ElementFactory::make("videoconvert").build().map_err(|_|())?;
  let scale=gst::ElementFactory::make("videoscale").build().map_err(|_|())?;
  let rate=gst::ElementFactory::make("videorate").property("drop-only",true).build().map_err(|_|())?;
  let filter=gst::ElementFactory::make("capsfilter").property("caps",gst::Caps::builder("video/x-raw").field("format","I420")
   .field("width",gst::IntRange::new(1i32,req.max_width as i32)).field("height",gst::IntRange::new(1i32,req.max_height as i32))
   .field("framerate",gst::Fraction::new(req.fps as i32,1)).build()).build().map_err(|_|())?;
  let encoder=gst::ElementFactory::make("vp8enc").build().map_err(|_|())?;
  let observer=if req.version==5 || req.version==6 || req.version==7 || req.version==8 {let value=crate::observe::Observer::default();value.attach(&source,&rate,&encoder,&filter)?;Some(value)} else {None};
  observe_encoded(&encoder,first,seen)?;
  let mux=gst::ElementFactory::make("webmmux").property("streamable",true).build().map_err(|_|())?;
  let sink=gst_app::AppSink::builder().build();if req.version==7 {sink.set_property("async",false);}sink.set_max_buffers(1);sink.set_drop(false);sink.set_wait_on_eos(false);
  let pipe=gst::Pipeline::default();let bus=pipe.bus().ok_or(())?;
  bus.set_sync_handler(move |_,message| {let event=match message.view(){gst::MessageView::Error(_)=>Some("error"),gst::MessageView::Eos(_)=>Some("eos"),_=>None};if let Some(e)=event{let _=events.try_send(e);}gst::BusSyncReply::Drop});
  #[cfg(feature="audio-foundation")]
  let crop=if req.version==7 {Some(crate::crop_preview::CropPreview::attach(&pipe,source.clone())?)} else {None};
  #[cfg(feature="audio-foundation")]
  let input=if let Some(branch)=crop.as_ref(){branch.output.clone()}else{pipe.add(&source).map_err(|_|())?;source.clone()};
  #[cfg(not(feature="audio-foundation"))]
  let input={if req.version==7{return Err(());}pipe.add(&source).map_err(|_|())?;source.clone()};
  pipe.add_many([&convert,&scale,&rate,&filter,&encoder,&mux,sink.upcast_ref()]).map_err(|_|())?;
  gst::Element::link_many([&input,&convert,&scale,&rate,&filter,&encoder,&mux,sink.upcast_ref()]).map_err(|_|())?;
  if play && pipe.set_state(gst::State::Playing).is_err(){let _=pipe.set_state(gst::State::Null);return Err(());}
  Ok(Self{pipe,sink,mux,caps:filter,rate,observer,#[cfg(feature="audio-foundation")] crop})
 }
 pub(crate) fn size(&self)->Option<(i32,i32)>{let caps=self.caps.static_pad("src")?.current_caps()?;let s=caps.structure(0)?;let w=s.get::<i32>("width").ok()?;let h=s.get::<i32>("height").ok()?;if w>0 && h>0 && w<=1920 && h<=1080 {Some((w,h))} else {None}}
}
#[cfg(feature="audio-foundation")]
impl Pipeline {
 pub(crate) fn reset_crop_epoch(&self,r:&crate::record_crop::Rect)->Result<(),()> {
  let branch=self.crop.as_ref().ok_or(())?;let observer=self.observer.as_ref().ok_or(())?;let mut reset=Ok(());
  branch.reset_epoch(&self.pipe,r,||{reset=observer.reset_epoch();})?;reset
 }
}
impl Pipeline {pub(crate) fn observations(&self)->Option<serde_json::Value>{self.observer.as_ref().map(|o|o.snapshot(&self.rate,self.size()))}}
impl Drop for Pipeline {fn drop(&mut self){let _=self.pipe.set_state(gst::State::Null);}}

struct AbortOnDrop(Arc<AtomicBool>);
impl Drop for AbortOnDrop { fn drop(&mut self) { self.0.store(true, Ordering::Release); } }
pub(crate) struct Output {pub(crate) bytes:u64,pub(crate) limit:bool,pub(crate) failed:bool}
pub(crate) fn write_output(sink:gst_app::AppSink,mut fd:File,max:u64,abort:Arc<AtomicBool>,finishing:Arc<AtomicBool>)->Output {
 let mut result=Output{bytes:0,limit:false,failed:false};
 loop {
  if abort.load(Ordering::Acquire){break;}
  if let Some(sample)=sink.try_pull_sample(gst::ClockTime::from_mseconds(100)) {
   let Some(buffer)=sample.buffer() else {result.failed=true;break;};
   let Ok(map)=buffer.map_readable() else {result.failed=true;break;};
   let bytes=map.as_slice();if result.bytes.checked_add(bytes.len() as u64).is_none_or(|n|n>max){result.limit=true;break;}
   if fd.write_all(bytes).is_err(){result.failed=true;break;}result.bytes+=bytes.len() as u64;
  } else if finishing.load(Ordering::Acquire) && sink.is_eos(){break;}
 }
 if fd.flush().is_err(){result.failed=true;}
 drop(fd);result
}

pub(crate) fn output_file(raw_fd:i32)->Option<File> {
 if raw_fd<3{return None;}
 // The host passes exactly this privately owned pipe descriptor with pass_fds.
 // Check the original inherited descriptor without reopening or blocking.
 // This runs before spawning any tasks, while the host-owned process has sole use.
 let flags=unsafe{libc::fcntl(raw_fd,libc::F_GETFL)};
 if flags<0 || (flags & libc::O_ACCMODE)!=libc::O_WRONLY {return None;}
 let mut info=std::mem::MaybeUninit::<libc::stat>::uninit();
 if unsafe{libc::fstat(raw_fd,info.as_mut_ptr())}!=0 {return None;}
 let info=unsafe{info.assume_init()};
 if (info.st_mode & libc::S_IFMT)!=libc::S_IFIFO {return None;}
 Some(unsafe{File::from_raw_fd(raw_fd)})
}

pub async fn run(raw_fd:i32, version:u8) {
 let Some(file)=output_file(raw_fd) else{return;};
 let (tx,mut rx)=mpsc::channel::<Option<Vec<u8>>>(1);
 tokio::spawn(async move {let mut input=BufReader::new(tokio::io::stdin());loop{let mut line=Vec::new();let read=(&mut input).take(8194).read_until(b'\n',&mut line).await;if !matches!(read,Ok(n) if n>0)||line.len()>8192||!line.ends_with(b"\n"){let _=tx.send(None).await;break;}if tx.send(Some(line)).await.is_err(){break;}}});
 let Some(Some(line))=rx.recv().await else{return;};let Ok(req)=serde_json::from_slice::<Start>(&line) else{return;};
 if req.version!=version||req.action!="start"||req.generation.len()!=32||!req.generation.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))||req.deadline_ms<=now()||req.deadline_ms>now().saturating_add(120000)||req.duration_ms==0||req.duration_ms>60000||req.fps==0||req.fps>30||req.max_width==0||req.max_width>1920||req.max_height==0||req.max_height>1080||req.max_bytes==0||req.max_bytes>67108864{return;}
 let deadline=tokio::time::sleep(Duration::from_millis(req.deadline_ms.saturating_sub(now())));tokio::pin!(deadline);
 let portal=tokio::select!{r=Screencast::new()=>match r{Ok(v)=>v,Err(_)=>return},_=rx.recv()=>return,_=&mut deadline=>return};
 let session=tokio::select!{r=portal.create_session(Default::default())=>match r{Ok(v)=>v,Err(_)=>return},_=rx.recv()=>return,_=&mut deadline=>return};
 let setup=async {
  let mut closed=session.receive_closed().await.map_err(|_|())?;
  let options=SelectSourcesOptions::default().set_cursor_mode(CursorMode::Embedded).set_sources(SourceType::Monitor|SourceType::Window).set_multiple(false).set_persist_mode(PersistMode::DoNot);
  tokio::select!{r=portal.select_sources(&session,options)=>{r.map_err(|_|())?.response().map_err(|_|())?;},_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(())}
  let response=tokio::select!{r=portal.start(&session,None,Default::default())=>r.map_err(|_|())?.response().map_err(|_|())?,_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(())};
  if response.streams().len()!=1{return Err(());}let stream=&response.streams()[0];let kind=match stream.source_type(){Some(SourceType::Monitor)=>"monitor",Some(SourceType::Window)=>"window",_=>"unknown"};
  let logical=stream.size().filter(|(w,h)|*w>0&&*h>0&&*w<=65536&&*h<=65536);
  let remote=tokio::select!{r=portal.open_pipe_wire_remote(&session,Default::default())=>r.map_err(|_|())?,_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(())};
  if req.deadline_ms.saturating_sub(now()) < req.duration_ms.saturating_add(5000) { return Err(()); }
  let (events_tx,mut events)=mpsc::channel(1);let (first_tx,mut first_rx)=mpsc::channel(1);let seen=Arc::new(AtomicBool::new(false));
  let pipeline=Pipeline::new(&remote,stream.pipe_wire_node_id(),&req,events_tx,first_tx,seen.clone(),true)?;
  let abort=Arc::new(AtomicBool::new(false));let _abort_on_drop=AbortOnDrop(abort.clone());let finishing=Arc::new(AtomicBool::new(false));
  let worker=tokio::task::spawn_blocking({let sink=pipeline.sink.clone();let abort=abort.clone();let finishing=finishing.clone();move||write_output(sink,file,req.max_bytes,abort,finishing)});tokio::pin!(worker);
  send(serde_json::json!({"version":version,"state":"starting","generation":req.generation,"source_type":kind,"logical_size":logical,"cursor":"embedded","audio":"none"}));
  let mut announced=false;
  let duration=tokio::time::sleep(Duration::from_millis(req.duration_ms));tokio::pin!(duration);
  let reason=loop { tokio::select!{
   biased;
   _=closed.next()=>break "source_lost", _=&mut deadline=>break "cancelled",
   event=events.recv()=>break if event==Some("error"){"source_lost"}else{"error"},
   signal=first_rx.recv(), if !announced=>{if signal.is_none(){return Err(());}announced=true;send(serde_json::json!({"version":version,"state":"recording","generation":req.generation,"source_type":kind,"logical_size":logical,"cursor":"embedded","audio":"none"}));},
   result=&mut worker=>{
    let output=result.map_err(|_|())?;
    let why=if output.limit{"output_limit"}else{"error"};
    let mut reply=serde_json::json!({"version":version,"state":"partial","generation":req.generation,"reason":why,"encoded_size":pipeline.size(),"first_pts_ns":null,"last_pts_ns":null,"timebase":"stream_nanoseconds","frames_in":null,"frames_encoded":null,"drops":null,"bytes_written":output.bytes});
    if let Some(observer)=&pipeline.observer {reply["observations"]=observer.snapshot(&pipeline.rate,pipeline.size());}send(reply);
    return Ok(());
   },
   line=rx.recv()=>{let Some(Some(line))=line else{return Err(());};let Ok(command)=serde_json::from_slice::<Command>(&line) else{return Err(());};if command.version!=version||command.generation!=req.generation{return Err(());}match command.action.as_str(){"stop"=>break "stopped","cancel"=>break "cancelled","frame"=>send(serde_json::json!({"version":version,"state":"unsupported","generation":req.generation,"feature":"latest_frame"})),_=>return Err(())}},
   _=&mut duration=>break "duration"
  }};
  let mut finalized=false;
  if reason=="stopped"||reason=="duration" {
   if pipeline.pipe.send_event(gst::event::Eos::new()) {
    let grace=tokio::time::sleep(Duration::from_secs(3));tokio::pin!(grace);
    finalized=tokio::select!{biased;_=closed.next()=>false,_=&mut deadline=>false,event=events.recv()=>event==Some("eos"),_=rx.recv()=>false,_=&mut grace=>false};
   }
  }
  if finalized {finishing.store(true,Ordering::Release);} else {abort.store(true,Ordering::Release);let _=pipeline.pipe.set_state(gst::State::Null);}
  let encoded_size=pipeline.size();
  let result=tokio::time::timeout(Duration::from_secs(2),&mut worker).await.map_err(|_|())?.map_err(|_|())?;
  let success=finalized&&seen.load(Ordering::Acquire)&&!result.limit&&!result.failed&&encoded_size.is_some()&&result.bytes>0;
  let terminal_reason=if result.limit{"output_limit"}else if result.failed||(!success&&(reason=="stopped"||reason=="duration")){"error"}else{reason};
  let mut reply=serde_json::json!({"version":version,"state":if success{"finalized"}else{"partial"},"generation":req.generation,"reason":terminal_reason,"encoded_size":encoded_size,"first_pts_ns":null,"last_pts_ns":null,"timebase":"stream_nanoseconds","frames_in":null,"frames_encoded":null,"drops":null,"bytes_written":result.bytes});
  if let Some(observer)=&pipeline.observer {reply["observations"]=observer.snapshot(&pipeline.rate,encoded_size);}send(reply);
  drop(pipeline);drop(remote);Ok::<(),()>(())
 };
 let _=setup.await;let _=tokio::time::timeout(Duration::from_secs(1),session.close()).await;
}

/// Record only the exact PipeWire remote and serial transferred from an already
/// authorized control session. The caller owns both inherited descriptors.
pub async fn run_control(raw_output:i32, raw_remote:i32, serial:u64) {
 if raw_remote<3 || raw_remote==raw_output || serial==0 {return;}
 let mut stat=std::mem::MaybeUninit::<libc::stat>::uninit();
 if unsafe{libc::fstat(raw_remote,stat.as_mut_ptr())}!=0 || (unsafe{stat.assume_init()}.st_mode & libc::S_IFMT)!=libc::S_IFSOCK {return;}
 let remote=unsafe{OwnedFd::from_raw_fd(raw_remote)};
 let Some(file)=output_file(raw_output) else{return;};
 let (tx,mut rx)=mpsc::channel::<Option<Vec<u8>>>(1);
 tokio::spawn(async move {let mut input=BufReader::new(tokio::io::stdin());loop{let mut line=Vec::new();let read=(&mut input).take(8194).read_until(b'\n',&mut line).await;if !matches!(read,Ok(n) if n>0)||line.len()>8192||!line.ends_with(b"\n"){let _=tx.send(None).await;break;}if tx.send(Some(line)).await.is_err(){break;}}});
 let Some(Some(line))=rx.recv().await else{return;};let Ok(req)=serde_json::from_slice::<Start>(&line) else{return;};
 if req.version!=8||req.action!="start"||req.generation.len()!=32||!req.generation.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))||req.deadline_ms<=now()||req.deadline_ms>now().saturating_add(120000)||req.duration_ms==0||req.duration_ms>60000||req.fps==0||req.fps>30||req.max_width==0||req.max_width>1920||req.max_height==0||req.max_height>1080||req.max_bytes==0||req.max_bytes>67108864||req.deadline_ms.saturating_sub(now())<req.duration_ms.saturating_add(5000){return;}
 let (events_tx,mut events)=mpsc::channel(1);let (first_tx,mut first_rx)=mpsc::channel(1);let seen=Arc::new(AtomicBool::new(false));
 let Ok(pipeline)=Pipeline::new_control(&remote,serial,&req,events_tx,first_tx,seen.clone()) else{return;};
 let abort=Arc::new(AtomicBool::new(false));let _abort_on_drop=AbortOnDrop(abort.clone());let finishing=Arc::new(AtomicBool::new(false));
 let worker=tokio::task::spawn_blocking({let sink=pipeline.sink.clone();let abort=abort.clone();let finishing=finishing.clone();let max=req.max_bytes;move||write_output(sink,file,max,abort,finishing)});tokio::pin!(worker);
 send(serde_json::json!({"version":8,"state":"starting","generation":req.generation,"source_type":"monitor","logical_size":null,"cursor":"embedded","audio":"none"}));
 let mut announced=false;let deadline=tokio::time::sleep(Duration::from_millis(req.deadline_ms.saturating_sub(now())));tokio::pin!(deadline);
 let duration=tokio::time::sleep(Duration::from_millis(req.duration_ms));tokio::pin!(duration);
 let reason=loop {tokio::select!{biased;
  _=&mut deadline=>break "cancelled",
  event=events.recv()=>break if event==Some("error"){"source_lost"}else{"error"},
  signal=first_rx.recv(),if !announced=>{if signal.is_none(){return;}announced=true;send(serde_json::json!({"version":8,"state":"recording","generation":req.generation,"source_type":"monitor","logical_size":null,"cursor":"embedded","audio":"none"}));},
  result=&mut worker=>{let Ok(output)=result else{return;};let mut reply=serde_json::json!({"version":8,"state":"partial","generation":req.generation,"reason":if output.limit{"output_limit"}else{"error"},"encoded_size":pipeline.size(),"first_pts_ns":null,"last_pts_ns":null,"timebase":"stream_nanoseconds","frames_in":null,"frames_encoded":null,"drops":null,"bytes_written":output.bytes});if let Some(obs)=pipeline.observations(){reply["observations"]=obs;}send(reply);return;},
  line=rx.recv()=>{let Some(Some(line))=line else{return;};let Ok(command)=serde_json::from_slice::<Command>(&line) else{return;};if command.version!=8||command.generation!=req.generation{return;}match command.action.as_str(){"stop"=>break "stopped","cancel"=>break "cancelled",_=>return,}},
  _=&mut duration=>break "duration"
 }};
 let mut finalized=false;
 if (reason=="stopped"||reason=="duration") && pipeline.pipe.send_event(gst::event::Eos::new()) {
  let grace=tokio::time::sleep(Duration::from_secs(3));tokio::pin!(grace);
  finalized=tokio::select!{biased;_=&mut deadline=>false,event=events.recv()=>event==Some("eos"),_=rx.recv()=>false,_=&mut grace=>false};
 }
 if finalized {finishing.store(true,Ordering::Release);}else{abort.store(true,Ordering::Release);let _=pipeline.pipe.set_state(gst::State::Null);}
 let size=pipeline.size();let Ok(Ok(result))=tokio::time::timeout(Duration::from_secs(2),&mut worker).await else{return;};
 let success=finalized&&seen.load(Ordering::Acquire)&&!result.limit&&!result.failed&&size.is_some()&&result.bytes>0;
 let terminal=if result.limit{"output_limit"}else if result.failed||(!success&&(reason=="stopped"||reason=="duration")){"error"}else{reason};
 let mut reply=serde_json::json!({"version":8,"state":if success{"finalized"}else{"partial"},"generation":req.generation,"reason":terminal,"encoded_size":size,"first_pts_ns":null,"last_pts_ns":null,"timebase":"stream_nanoseconds","frames_in":null,"frames_encoded":null,"drops":null,"bytes_written":result.bytes});
 if let Some(obs)=pipeline.observations(){reply["observations"]=obs;}send(reply);
}

#[cfg(test)]
mod tests {
 use super::*;
 use std::io::Read;
 fn run_synthetic(limit:u64, expect_eos:bool)->(Output,Vec<u8>) {
  gst::init().unwrap();
  let pipeline=gst::parse::launch("videotestsrc num-buffers=3 ! video/x-raw,width=64,height=48,framerate=15/1 ! videoconvert ! vp8enc name=encoder ! webmmux streamable=true ! appsink name=out").unwrap().downcast::<gst::Pipeline>().unwrap();
  let (first_tx,mut first_rx)=mpsc::channel(1);let seen=Arc::new(AtomicBool::new(false));
  observe_encoded(&pipeline.by_name("encoder").unwrap(),first_tx,seen.clone()).unwrap();
  let sink=pipeline.by_name("out").unwrap().downcast::<gst_app::AppSink>().unwrap();sink.set_max_buffers(1);sink.set_drop(false);sink.set_wait_on_eos(false);
  let mut fds=[0i32;2];assert_eq!(unsafe{libc::pipe(fds.as_mut_ptr())},0);
  let mut read=unsafe{File::from_raw_fd(fds[0])};let write=unsafe{File::from_raw_fd(fds[1])};
  let reader=std::thread::spawn(move||{let mut bytes=Vec::new();read.read_to_end(&mut bytes).unwrap();bytes});
  let abort=Arc::new(AtomicBool::new(false));let finishing=Arc::new(AtomicBool::new(false));
  pipeline.set_state(gst::State::Playing).unwrap();
  let worker=std::thread::spawn({let abort=abort.clone();let finishing=finishing.clone();move||write_output(sink,write,limit,abort,finishing)});
  if expect_eos {let bus=pipeline.bus().unwrap();let message=bus.timed_pop_filtered(gst::ClockTime::from_seconds(5),&[gst::MessageType::Eos,gst::MessageType::Error]).expect("synthetic EOS timeout");assert!(matches!(message.view(),gst::MessageView::Eos(_)));finishing.store(true,Ordering::Release);}
  let result=worker.join().unwrap();if expect_eos {assert!(seen.load(Ordering::Acquire));assert_eq!(first_rx.try_recv(),Ok(()));}let _=pipeline.set_state(gst::State::Null);let bytes=reader.join().unwrap();(result,bytes)
 }
 #[test]
 fn synthetic_webm_eos_and_hard_cap() {
  let (normal,bytes)=run_synthetic(1_000_000,true);assert!(!normal.limit&&!normal.failed&&normal.bytes==bytes.len() as u64);assert!(bytes.starts_with(&[0x1a,0x45,0xdf,0xa3]));
  let (limited,bytes)=run_synthetic(1,false);assert!(limited.limit&&!limited.failed&&limited.bytes<=1&&bytes.len()<=1);
 }
}
