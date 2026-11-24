//! Private v6 original-child controller. No capability is advertised until validated.
use std::{cell::RefCell,io::{self,BufRead,Read,Write},sync::{Arc,atomic::{AtomicBool,Ordering}},time::Duration};
use ashpd::desktop::{PersistMode,screencast::{CursorMode,Screencast,SelectSourcesOptions,SourceType}};
use futures_util::StreamExt;
use gstreamer as gst;
use gstreamer_app as gst_app;
use gst::prelude::*;
use serde::Deserialize;
use sha2::{Digest,Sha256};
use tokio::sync::mpsc;
use crate::{audio_registry::RegistryOwner,record};

#[derive(Deserialize)] #[serde(deny_unknown_fields)]
struct Intent {microphone:bool,system_output:bool}
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
struct Start {version:u8,action:String,generation:String,deadline_ms:u64,duration_ms:u64,fps:u32,max_width:u32,max_height:u32,max_bytes:u64,audio_intent:Intent,#[serde(default)] crop_intent:Option<bool>}
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
struct Select {version:u8,action:String,generation:String,selection_id:String,selection_digest:String,microphone:Option<String>,system_output:Option<String>}
#[derive(Deserialize)] #[serde(deny_unknown_fields)]
struct Command {version:u8,action:String,generation:String}
fn hex(s:&str,n:usize)->bool{s.len()==n&&s.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c))}
fn valid_start(r:&Start)->bool {(r.version==6&&r.crop_intent.is_none()&&(r.audio_intent.microphone||r.audio_intent.system_output)||r.version==7&&r.crop_intent==Some(true))&&r.action=="start"&&hex(&r.generation,32)&&r.deadline_ms>record::now()&&r.deadline_ms<=record::now().saturating_add(120_000)&&r.duration_ms>0&&r.duration_ms<=60_000&&r.fps>0&&r.fps<=30&&r.max_width>0&&r.max_width<=1920&&r.max_height>0&&r.max_height<=1080&&r.max_bytes>0&&r.max_bytes<=67_108_864}
fn valid_select(r:&Start,s:&Select)->bool {
 if s.version!=6||s.action!="select"||s.generation!=r.generation||!hex(&s.selection_id,32)||!hex(&s.selection_digest,64)||s.microphone.as_ref().is_some_and(|v|!hex(v,32)||!r.audio_intent.microphone)||s.system_output.as_ref().is_some_and(|v|!hex(v,32)||!r.audio_intent.system_output){return false;}
 let value=serde_json::json!(["openclank.recording.audio-selection.v1",s.generation,s.selection_id,s.microphone,s.system_output]);
 let Ok(bytes)=serde_json::to_vec(&value) else{return false;};format!("{:x}",Sha256::digest(bytes))==s.selection_digest
}
fn send(value:serde_json::Value)->Result<(),()> {let data=serde_json::to_vec(&value).map_err(|_|())?;if data.len()+1>8192{return Err(());}let mut out=io::stdout().lock();out.write_all(&data).and_then(|_|out.write_all(b"\n")).and_then(|_|out.flush()).map_err(|_|())}
struct Abort(Arc<AtomicBool>);impl Drop for Abort{fn drop(&mut self){self.0.store(true,Ordering::Release);}}
struct Track {kind:&'static str,prepared:crate::audio_registry::Prepared,src:gst_app::AppSrc}
fn track_values(tracks:&[Track])->Result<Vec<serde_json::Value>,()> {tracks.iter().map(|t|{let mut v=serde_json::to_value(t.prepared.stats()).map_err(|_|())?;v["kind"]=t.kind.into();Ok(v)}).collect()}

pub(crate) async fn run(raw_fd:i32, preview_fd:Option<i32>) {
 let Some(file)=record::output_file(raw_fd) else{return;};
 let preview=match preview_fd {Some(fd) if fd!=raw_fd=>match record::output_file(fd){Some(f)=>Some(f),None=>return},Some(_)=>return,None=>None};
 let (tx,mut rx)=mpsc::channel::<Option<Vec<u8>>>(1);
 std::thread::spawn(move||{let stdin=io::stdin();let mut input=stdin.lock();loop{let mut line=Vec::new();let read=(&mut input).take(8194).read_until(b'\n',&mut line);if !matches!(read,Ok(n) if n>0)||line.len()>8192||!line.ends_with(b"\n"){let _=tx.blocking_send(None);break;}if tx.blocking_send(Some(line)).is_err(){break;}}});
 let Some(Some(line))=rx.recv().await else{return;};let Ok(req)=serde_json::from_slice::<Start>(&line) else{return;};if !valid_start(&req)|| (req.version==7)!=preview.is_some(){return;}
 let deadline=tokio::time::sleep(Duration::from_millis(req.deadline_ms.saturating_sub(record::now())));tokio::pin!(deadline);
 let portal=tokio::select!{biased;_=rx.recv()=>return,_=&mut deadline=>return,r=Screencast::new()=>match r{Ok(v)=>v,Err(_)=>return}};
 let session=tokio::select!{biased;_=rx.recv()=>return,_=&mut deadline=>return,r=portal.create_session(Default::default())=>match r{Ok(v)=>v,Err(_)=>return}};
 let setup=async {
  let mut closed=session.receive_closed().await.map_err(|_|())?;
  let options=SelectSourcesOptions::default().set_cursor_mode(CursorMode::Embedded).set_sources(SourceType::Monitor|SourceType::Window).set_multiple(false).set_persist_mode(PersistMode::DoNot);
  tokio::select!{biased;_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(()),r=portal.select_sources(&session,options)=>{r.map_err(|_|())?.response().map_err(|_|())?;}}
  let response=tokio::select!{biased;_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(()),r=portal.start(&session,None,Default::default())=>r.map_err(|_|())?.response().map_err(|_|())?};
  if response.streams().len()!=1{return Err(());}let stream=&response.streams()[0];let source_type=match stream.source_type(){Some(SourceType::Monitor)=>"monitor",Some(SourceType::Window)=>"window",_=>"unknown"};
  let logical=stream.size().filter(|(w,h)|*w>0&&*h>0&&*w<=65536&&*h<=65536);
  let remote=tokio::select!{biased;_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(()),r=portal.open_pipe_wire_remote(&session,Default::default())=>r.map_err(|_|())?};
  let video=record::Start{version:req.version,action:"start".into(),generation:req.generation.clone(),deadline_ms:req.deadline_ms,duration_ms:req.duration_ms,fps:req.fps,max_width:req.max_width,max_height:req.max_height,max_bytes:req.max_bytes};
  let (events_tx,mut events)=mpsc::channel(1);let (first_tx,mut first_rx)=mpsc::channel(1);let seen=Arc::new(AtomicBool::new(false));
  let mut pipeline=record::Pipeline::new(&remote,stream.pipe_wire_node_id(),&video,events_tx,first_tx,seen.clone(),false)?;
  let mut preview_basis=None;
  let mut preview_info=None;
  if req.version==7 {
   pipeline.pipe.set_state(gst::State::Playing).map_err(|_|())?;
   let branch=pipeline.crop.as_mut().ok_or(())?;
   // Bounded pull; stdin remains serviced by the original reader thread.
   let item=branch.sample()?.ok_or(())?;
let digest=format!("{:x}",Sha256::digest(&item.png));
   let basis=format!("{:x}",Sha256::digest(format!("{}:{}:{}:{}",req.generation,digest,item.source.0,item.source.1).as_bytes()))[..32].to_string();
   let mut preview_file=preview.ok_or(())?;use std::os::fd::AsRawFd;let fd=preview_file.as_raw_fd();let flags=unsafe{libc::fcntl(fd,libc::F_GETFL)};if flags<0||unsafe{libc::fcntl(fd,libc::F_SETFL,flags|libc::O_NONBLOCK)}<0{return Err(());}
   let mut written=0;while written<item.png.len(){if record::now()>=req.deadline_ms||!matches!(rx.try_recv(),Err(mpsc::error::TryRecvError::Empty)){return Err(());}match preview_file.write(&item.png[written..]){Ok(0)=>return Err(()),Ok(n)=>written+=n,Err(e) if e.kind()==io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(2)),Err(_)=>return Err(())}}drop(preview_file);
   preview_info=Some(serde_json::json!({"basis_id":basis,"source_width":item.source.0,"source_height":item.source.1,"preview_width":item.pixels.0,"preview_height":item.pixels.1,"bytes":item.png.len(),"sha256":digest,"mime":"image/png"}));
   preview_basis=Some((basis,item.source));
  }
  let registry=if req.audio_intent.microphone||req.audio_intent.system_output{Some(RegistryOwner::open(&req.generation)?)}else{None};
  let choices=if let Some(owner)=registry.as_ref(){owner.snapshot(Duration::from_secs(2))?}else{Vec::new()};
  let choices:Vec<_>=choices.into_iter().filter(|c|(c.kind=="microphone"&&req.audio_intent.microphone)||(c.kind=="system_output"&&req.audio_intent.system_output)).collect();
  let mut choices_reply=serde_json::json!({"version":req.version,"state":"choices","generation":req.generation,"choices":choices});if let Some(info)=preview_info.as_ref(){choices_reply["preview"]=info.clone();}send(choices_reply)?;
  let line=tokio::select!{biased;_=closed.next()=>return Err(()),_=&mut deadline=>return Err(()),line=rx.recv()=>{let Some(Some(v))=line else{return Err(());};v}};
  let (select,crop_rect,crop_ack)=if req.version==7 {
   let v:crate::record_crop::Selection=serde_json::from_slice(&line).map_err(|_|())?;let (basis,size)=preview_basis.as_ref().ok_or(())?;
   let rect=v.validate(&req.generation,basis,size.0,size.1,req.audio_intent.microphone,req.audio_intent.system_output)?;
   let crop=serde_json::to_value(&v.crop).map_err(|_|())?;
   (Select{version:7,action:v.action,generation:v.generation,selection_id:v.selection_id,selection_digest:v.selection_digest,microphone:v.microphone,system_output:v.system_output},Some(rect),Some(crop))
  }else{let v:Select=serde_json::from_slice(&line).map_err(|_|())?;if !valid_select(&req,&v){return Err(());}(v,None,None)};
  let bindings=if let Some(owner)=registry.as_ref(){owner.poll()?;owner.freeze(&req.generation,select.microphone.as_deref(),select.system_output.as_deref())?}else{Vec::new()};
  if req.deadline_ms.saturating_sub(record::now())<req.duration_ms.saturating_add(5000){return Err(());}
  let mut selected=Vec::new();
  for (kind,binding) in bindings {
   let pending=RefCell::new(&mut rx);let current=||record::now()<req.deadline_ms&&matches!(pending.borrow_mut().try_recv(),Err(mpsc::error::TryRecvError::Empty));
   let prepared=registry.as_ref().ok_or(())?.prepare_frozen(binding,Duration::from_secs(2),&current)?;drop(pending);selected.push((kind,prepared));
  }
  tokio::select!{biased;_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(()),_=tokio::task::yield_now()=>{}}
  if req.version==7 {pipeline.pipe.set_state(gst::State::Paused).map_err(|_|())?;}
  let mut tracks=Vec::new();for(kind,prepared) in selected {let channels=prepared.stats();let value=serde_json::to_value(channels).map_err(|_|())?;let n=value.get("channels").and_then(|v|v.as_u64()).and_then(|v|u32::try_from(v).ok()).ok_or(())?;let src=crate::audio_pcm::attach(&pipeline.pipe,&pipeline.mux,n)?;tracks.push(Track{kind,prepared,src});}
  if let Some(rect)=crop_rect.as_ref(){pipeline.reset_crop_epoch(rect)?;pipeline.crop.as_ref().ok_or(())?.open_gate()?;}else if pipeline.pipe.set_state(gst::State::Playing).is_err(){return Err(());}let active=||record::now()<req.deadline_ms;
  for track in &tracks {track.prepared.activate(registry.as_ref().ok_or(())?,&active)?;track.prepared.calibrate(&pipeline.pipe)?;track.prepared.calibrate(&pipeline.pipe)?;}
  tokio::select!{biased;_=closed.next()=>return Err(()),_=rx.recv()=>return Err(()),_=&mut deadline=>return Err(()),_=tokio::task::yield_now()=>{}}
  let kinds:Vec<_>=tracks.iter().map(|t|t.kind).collect();let audio=if tracks.is_empty(){"none"}else{"selected"};
  let mut ack=serde_json::json!({"version":req.version,"state":"selection_accepted","generation":req.generation,"selection_id":select.selection_id,"selection_digest":select.selection_digest,"selected_kinds":kinds});if let Some(crop)=crop_ack.as_ref(){ack["crop"]=crop.clone();}send(ack)?;
  send(serde_json::json!({"version":req.version,"state":"starting","generation":req.generation,"source_type":source_type,"logical_size":logical,"cursor":"embedded","audio":audio}))?;
  let abort=Arc::new(AtomicBool::new(false));let _abort=Abort(abort.clone());let finishing=Arc::new(AtomicBool::new(false));
  let worker=tokio::task::spawn_blocking({let sink=pipeline.sink.clone();let abort=abort.clone();let finishing=finishing.clone();let max=req.max_bytes;move||record::write_output(sink,file,max,abort,finishing)});tokio::pin!(worker);
  let mut announced=false;let duration=tokio::time::sleep(Duration::from_millis(req.duration_ms));tokio::pin!(duration);
  let mut pump=tokio::time::interval(Duration::from_millis(1));pump.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
  let mut early=None;
  let mut reason=loop {tokio::select!{biased;
   _=closed.next()=>break "source_lost",_=&mut deadline=>break "cancelled",
   event=events.recv()=>break if event==Some("error"){"source_lost"}else{"error"},
   line=rx.recv()=>{let Some(Some(line))=line else{break "cancelled";};let Ok(cmd)=serde_json::from_slice::<Command>(&line) else{break "cancelled";};if cmd.version!=req.version||cmd.generation!=req.generation{break "cancelled";}match cmd.action.as_str(){"stop"=>break "stopped","cancel"=>break "cancelled","frame"=>{send(serde_json::json!({"version":req.version,"state":"unsupported","generation":req.generation,"feature":"latest_frame"}))?;},_=>break "cancelled"}},
   signal=first_rx.recv(),if !announced=>{if signal.is_none()||pipeline.crop.as_ref().is_some_and(|c|!c.fresh_segment()||!c.basis_current()){break "error";}announced=true;send(serde_json::json!({"version":req.version,"state":"recording","generation":req.generation,"source_type":source_type,"logical_size":logical,"cursor":"embedded","audio":audio}))?;},
   result=&mut worker=>{early=Some(result.map_err(|_|())?);break if early.as_ref().is_some_and(|r|r.limit){"output_limit"}else{"error"};},
   _=pump.tick(),if !tracks.is_empty()||pipeline.crop.is_some()=>{let current=||record::now()<req.deadline_ms;if pipeline.crop.as_ref().is_some_and(|c|!c.basis_current())||registry.as_ref().is_some_and(|r|r.poll().is_err())||tracks.iter().any(|t|registry.as_ref().is_none_or(|r|t.prepared.feed(r,&pipeline.pipe,&t.src,&current).is_err())){break "source_lost";}},
   _=&mut duration=>break "duration"
  }};
  let mut finalized=false;
  if reason=="stopped"||reason=="duration" {
   let current=||record::now()<req.deadline_ms;
   if tracks.iter().any(|t|registry.as_ref().is_none_or(|r|t.prepared.stop_audio(r,&pipeline.pipe,&t.src,&current).is_err())){reason="source_lost";}
   let complete=tracks.iter().all(|t|{let Ok(v)=serde_json::to_value(t.prepared.stats()) else{return false;};v.get("eos")==Some(&serde_json::json!(true))&&v.get("submitted_frames").and_then(|v|v.as_u64()).is_some_and(|n|n>0)&&v.get("first_running_ns").and_then(|v|v.as_str()).is_some_and(|n|n.parse::<u64>().is_ok())&&v.get("last_running_ns").and_then(|v|v.as_str()).is_some_and(|n|n.parse::<u64>().is_ok())&&v.get("queued_frames").and_then(|v|v.as_u64()).is_some_and(|n|n>0&&v.get("submitted_frames").and_then(|v|v.as_u64()).zip(v.get("pre_start_discarded_frames").and_then(|v|v.as_u64())).and_then(|(submitted,discarded)|submitted.checked_add(discarded))==Some(n))});
   if !complete{reason="source_lost";}
   if let Some(branch)=pipeline.crop.as_ref(){branch.prepare_eos();}
   if (reason=="stopped"||reason=="duration")&&pipeline.pipe.send_event(gst::event::Eos::new()) {
    let grace=tokio::time::sleep(Duration::from_secs(3));tokio::pin!(grace);
    finalized=tokio::select!{biased;_=closed.next()=>false,_=&mut deadline=>false,_=rx.recv()=>false,event=events.recv()=>event==Some("eos"),_=&mut grace=>false};
   }
  }
  if finalized {finishing.store(true,Ordering::Release);} else {for track in &tracks{track.prepared.cancel_audio();}abort.store(true,Ordering::Release);let _=pipeline.pipe.set_state(gst::State::Null);}
  let result=if let Some(result)=early{result}else{tokio::time::timeout(Duration::from_secs(2),&mut worker).await.map_err(|_|())?.map_err(|_|())?};
  let size=pipeline.size();let success=finalized&&seen.load(Ordering::Acquire)&&pipeline.crop.as_ref().is_none_or(|c|c.fresh_segment()&&c.basis_current())&&!result.limit&&!result.failed&&size.is_some()&&result.bytes>0;
  let why=if result.limit{"output_limit"}else if result.failed||(!success&&(reason=="stopped"||reason=="duration")){"error"}else{reason};
  let mut terminal=serde_json::json!({"version":req.version,"state":if success{"finalized"}else{"partial"},"generation":req.generation,"reason":why,"encoded_size":size,"first_pts_ns":null,"last_pts_ns":null,"timebase":"stream_nanoseconds","frames_in":null,"frames_encoded":null,"drops":null,"bytes_written":result.bytes,"observations":pipeline.observations().ok_or(())?,"selection_id":select.selection_id,"selection_digest":select.selection_digest,"tracks":track_values(&tracks)?});
  if let Some(rect)=crop_rect.as_ref(){let (basis,source)=preview_basis.as_ref().ok_or(())?;let (w,h)=size.ok_or(())?;terminal["crop"]=serde_json::json!({"basis_id":basis,"source_width":source.0,"source_height":source.1,"rect":rect,"output_width":w,"output_height":h,"transform":{"x_numerator":w,"x_denominator":rect.width,"y_numerator":h,"y_denominator":rect.height},"basis":"source_buffer","pixel_aspect":"1:1","orientation":"identity"});}send(terminal)?;
  Ok::<(),()>(())
 };
 let _=setup.await;let _=tokio::time::timeout(Duration::from_secs(1),session.close()).await;
}

#[cfg(test)] mod tests {
 use super::*;
 #[test] fn synthetic_selection_and_mixed_media() {
  let req=Start{version:6,action:"start".into(),generation:"a".repeat(32),deadline_ms:record::now()+30_000,duration_ms:1000,fps:15,max_width:1920,max_height:1080,max_bytes:100000,audio_intent:Intent{microphone:true,system_output:true},crop_intent:None};
  let mut selection=Select{version:6,action:"select".into(),generation:"a".repeat(32),selection_id:"b".repeat(32),selection_digest:"a5716fe9394e0cf2e5c4bab3634e0e76f9361ded15ff6f32c772c9348db26787".into(),microphone:Some("c".repeat(32)),system_output:None};
  assert!(crate::audio_registry::synthetic_dual_freeze());assert!(valid_start(&req)&&valid_select(&req,&selection));selection.selection_digest.replace_range(0..1,"b");assert!(!valid_select(&req,&selection));
  selection.microphone=None;selection.selection_digest="04b39fb8faebdb535285d45cba29e530756b8e0136d3ca82cf4b302b449c1834".into();assert!(valid_select(&req,&selection));
  gst::init().unwrap();let pipe=gst::parse::launch("videotestsrc num-buffers=3 ! video/x-raw,width=64,height=48,framerate=15/1 ! videoconvert ! vp8enc ! webmmux name=mux streamable=true ! appsink name=out sync=false").unwrap().downcast::<gst::Pipeline>().unwrap();
  let mux=pipe.by_name("mux").unwrap();let one=crate::audio_pcm::attach(&pipe,&mux,1).unwrap();let two=crate::audio_pcm::attach(&pipe,&mux,1).unwrap();let sink=pipe.by_name("out").unwrap().downcast::<gst_app::AppSink>().unwrap();pipe.set_state(gst::State::Playing).unwrap();
  for src in [&one,&two] {let mut b=gst::Buffer::from_mut_slice(vec![0u8;1920]);let b=b.get_mut().unwrap();b.set_pts(gst::ClockTime::ZERO);b.set_duration(gst::ClockTime::from_mseconds(20));src.push_buffer(b.to_owned()).unwrap();src.end_of_stream().unwrap();}
  let mut bytes=Vec::new();while let Some(sample)=sink.try_pull_sample(gst::ClockTime::from_seconds(3)){bytes.extend_from_slice(sample.buffer().unwrap().map_readable().unwrap().as_slice());}
  assert!(bytes.starts_with(&[0x1a,0x45,0xdf,0xa3]));assert!(bytes.windows(6).filter(|w|*w==b"A_OPUS").count()>=2);assert!(bytes.windows(5).any(|w|w==b"V_VP8"));assert!(sink.is_eos());pipe.set_state(gst::State::Null).unwrap();
 }
}
