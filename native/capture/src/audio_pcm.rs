//! Private, non-RT PipeWire PCM bridge. Callers own scope, loop, and pipeline lifetime.
use std::{cell::{Cell,RefCell},collections::VecDeque,rc::Rc};
use gstreamer as gst;
use gstreamer_app as gst_app;
use gst::prelude::*;
use pipewire as pw;
use crate::audio_foundation::{Calibration,PcmQueue,Error as TimingError};

const SAFE_MAX:u64=9_007_199_254_740_991;
#[derive(Clone,Default,serde::Serialize)]
pub(crate) struct TrackStats {stage:&'static str,assurance:&'static str,sample_rate:Option<u32>,channels:Option<u32>,queued_frames:u64,submitted_frames:u64,first_running_ns:Option<String>,last_running_ns:Option<String>,max_clock_uncertainty_ns:Option<String>,last_graph_delay_ns:Option<String>,discontinuities:u64,missing_metadata:u64,header_buffers:u64,header_absent_buffers:u64,pre_start_discarded_frames:u64,eos:bool}
fn add_count(value:u64,add:u64)->Result<u64,()> {value.checked_add(add).filter(|v|*v<=SAFE_MAX).ok_or(())}
fn tightly_packed(stride:i32,channels:u32)->bool {stride==0 || stride==channels as i32*2}
fn delay_ns(t:Timing)->Result<i64,()> {if t.num==0||t.den==0{return Err(());}let n=i128::from(t.delay).checked_mul(i128::from(t.num)).and_then(|x|x.checked_mul(1_000_000_000)).ok_or(())?/i128::from(t.den);i64::try_from(n).map_err(|_|())}
#[derive(Clone,Copy)]
struct Timing {cycle:u64,now:u64,delay_now:u64,delay:i64,num:u32,den:u32,frames:u32}
struct Queue {pcm:PcmQueue,timing:VecDeque<Timing>,last_seq:Option<u64>,last_ticks:Option<u64>,failed:bool,accepting:bool,last_end:Option<u64>,anchor:Option<(u64,u64)>,stats:TrackStats}
pub(crate) struct AudioPcm {queue:RefCell<Queue>,gate:Rc<Cell<bool>>,channels:u32,calibration:RefCell<Option<Calibration>>}
impl AudioPcm {
 pub(crate) fn new(gate:Rc<Cell<bool>>,channels:u32)->Result<Rc<Self>,()> {
  if !(1..=2).contains(&channels){return Err(());}Ok(Rc::new(Self{queue:RefCell::new(Queue{pcm:PcmQueue::new(),timing:VecDeque::with_capacity(32),last_seq:None,last_ticks:None,failed:false,accepting:true,last_end:None,anchor:None,stats:TrackStats{stage:"pcm_appsrc_submission",assurance:"software_correlated_estimate",..TrackStats::default()}}),gate,channels,calibration:RefCell::new(None)}))
 }
 fn fail(&self){self.gate.set(false);self.queue.borrow_mut().failed=true;}
 fn missing(&self){let mut q=self.queue.borrow_mut();match add_count(q.stats.missing_metadata,1){Ok(n)=>q.stats.missing_metadata=n,Err(_)=>q.failed=true,}self.gate.set(false);}
 pub(crate) fn stats(&self)->TrackStats {self.queue.borrow().stats.clone()}
 pub(crate) fn mark_negotiated(&self){let mut q=self.queue.borrow_mut();q.stats.sample_rate=Some(48_000);q.stats.channels=Some(self.channels);}
 fn accept(&self,data:Vec<u8>,timing:Timing,header:Option<(u64,bool)>,ticks:u64)->Result<(),()> {
  let mut q=self.queue.borrow_mut();if !self.gate.get()||q.failed||!q.accepting{return Err(());}
  let (seq,discontinuous)=match header{Some((seq,discontinuous))=>{q.stats.header_buffers=add_count(q.stats.header_buffers,1)?;(Some(seq),discontinuous)},None=>{q.stats.header_absent_buffers=add_count(q.stats.header_absent_buffers,1)?;(None,false)}};
  if discontinuous {match add_count(q.stats.discontinuities,1){Ok(n)=>q.stats.discontinuities=n,Err(_)=>{q.failed=true;self.gate.set(false);return Err(());}}}
  if timing.cycle==0||timing.cycle>timing.now||timing.delay_now>timing.now||timing.num==0||timing.den==0||data.is_empty()||data.len()%(self.channels as usize*2)!=0||timing.frames as usize!=data.len()/(self.channels as usize*2)||seq.zip(q.last_seq).is_some_and(|(seq,x)|seq<=x)||q.last_ticks.is_some_and(|x|ticks<x)||(q.last_seq.is_some()&&discontinuous)||q.timing.len()>=32 {q.failed=true;self.gate.set(false);return Err(());}
  let queued=match add_count(q.stats.queued_frames,u64::from(timing.frames)){Ok(n)=>n,Err(_)=>{q.failed=true;self.gate.set(false);return Err(());}};
  let delay=match delay_ns(timing){Ok(n)=>n,Err(_)=>{q.failed=true;self.gate.set(false);return Err(());}};
  if q.pcm.push(data).is_err(){q.failed=true;self.gate.set(false);return Err(());}q.timing.push_back(timing);q.stats.queued_frames=queued;q.stats.last_graph_delay_ns=Some(delay.to_string());q.last_seq=seq;q.last_ticks=Some(ticks);Ok(())
 }
 /// Before opening admission, return already-queued transport buffers without reading them.
 pub(crate) fn drain_before_gate(&self,stream:&pw::stream::Stream)->Result<(),()> {
  if self.gate.get(){return Err(());}for _ in 0..64 {unsafe{let ptr=pw::sys::pw_stream_dequeue_buffer(stream.as_raw_ptr());if ptr.is_null(){return Ok(());}if pw::sys::pw_stream_queue_buffer(stream.as_raw_ptr(),ptr)<0{self.fail();return Err(());}}}self.fail();Err(())
 }
 /// Invoked from PipeWire's non-RT process callback; ownership of every raw buffer is returned.
 pub(crate) fn process(&self,stream:&pw::stream::Stream) {
  unsafe {
   let ptr=pw::sys::pw_stream_dequeue_buffer(stream.as_raw_ptr());if ptr.is_null(){return;}
   if !self.gate.get()||!self.queue.borrow().accepting {if pw::sys::pw_stream_queue_buffer(stream.as_raw_ptr(),ptr)<0{self.fail();}return;}
   let result=(||->Result<(),()>{
    let raw=&*ptr;let spa=match raw.buffer.as_ref(){Some(x)=>x,None=>{self.missing();return Err(());}};if spa.n_datas!=1||spa.datas.is_null()||spa.n_metas>32||(spa.n_metas>0&&spa.metas.is_null()){self.missing();return Err(());}
    let mut header=None;if spa.n_metas>0 {for m in std::slice::from_raw_parts(spa.metas,spa.n_metas as usize){if m.type_==pw::spa::sys::SPA_META_Header {if header.is_some() || (m.size as usize)<std::mem::size_of::<pw::spa::sys::spa_meta_header>() || m.data.is_null(){return Err(());}let h=&*(m.data as *const pw::spa::sys::spa_meta_header);if h.flags & (pw::spa::sys::SPA_META_HEADER_FLAG_CORRUPTED|pw::spa::sys::SPA_META_HEADER_FLAG_GAP)!=0{return Err(());}header=Some((h.seq,h.flags&pw::spa::sys::SPA_META_HEADER_FLAG_DISCONT!=0));}}}
    let d=&*spa.datas;let chunk=d.chunk.as_ref().ok_or(())?;if d.data.is_null()||chunk.flags!=0||chunk.size==0||!tightly_packed(chunk.stride,self.channels){return Err(());}
    let start=chunk.offset as usize;let end=start.checked_add(chunk.size as usize).ok_or(())?;if end>d.maxsize as usize||end-start>48_000{return Err(());}
    let sample=std::slice::from_raw_parts((d.data as *const u8).add(start),end-start).to_vec();let frame_bytes=self.channels as usize*2;if sample.len()%frame_bytes!=0{return Err(());}
    let mut snap=std::mem::MaybeUninit::<pw::sys::pw_time>::zeroed();if pw::sys::pw_stream_get_time_n(stream.as_raw_ptr(),snap.as_mut_ptr(),std::mem::size_of::<pw::sys::pw_time>())<0{self.missing();return Err(());}let snap=snap.assume_init();let now=pw::sys::pw_stream_get_nsec(stream.as_raw_ptr());
    let delay_now=match u64::try_from(snap.now){Ok(n)=>n,Err(_)=>{self.missing();return Err(());}};let timing=Timing{cycle:raw.time,now,delay_now,delay:snap.delay,num:snap.rate.num,den:snap.rate.denom,frames:(sample.len()/frame_bytes) as u32};
    self.accept(sample,timing,header,snap.ticks)
   })();
   let returned=pw::sys::pw_stream_queue_buffer(stream.as_raw_ptr(),ptr);if result.is_err()||returned<0{self.fail();}
  }
 }
 pub(crate) fn calibrate(&self,stream:&pw::stream::Stream,clock:&gst::Clock,base:u64)->Result<(),()> {
  let p1=unsafe{pw::sys::pw_stream_get_nsec(stream.as_raw_ptr())};let gst=clock.time().nseconds();let p2=unsafe{pw::sys::pw_stream_get_nsec(stream.as_raw_ptr())};
  let identity=clock.as_ptr() as usize as u64;let previous=*self.calibration.borrow();let next=Calibration::sample(p1,gst,p2,base,identity,previous).map_err(|_|())?;*self.calibration.borrow_mut()=Some(next);Ok(())
 }
 pub(crate) fn feed(&self,src:&gst_app::AppSrc,now:u64)->Result<(),()> {
  if !self.gate.get(){return Err(());}let calibration=match *self.calibration.borrow(){Some(c)=>c,None=>{self.fail();return Err(());}};let mut q=self.queue.borrow_mut();if q.failed{return Err(());}
  while let Some(t)=q.timing.pop_front(){let data=q.pcm.pop().ok_or(())?;if now<t.cycle||now-t.cycle>250_000_000 {drop(q);self.fail();return Err(());}
   let (pts,_duration)=match calibration.map(t.cycle,now,t.delay_now,t.delay,t.num,t.den,t.frames,48_000){Ok(x)=>x,Err(TimingError::PreStart) if q.stats.submitted_frames==0=>{q.stats.pre_start_discarded_frames=match add_count(q.stats.pre_start_discarded_frames,u64::from(t.frames)){Ok(n)=>n,Err(_)=>{drop(q);self.fail();return Err(());}};continue;},Err(_)=>{drop(q);self.fail();return Err(());}};
   let submitted=match add_count(q.stats.submitted_frames,u64::from(t.frames)){Ok(n)=>n,Err(_)=>{drop(q);self.fail();return Err(());}};
   let uncertainty=calibration.uncertainty_ns();let (anchor,initial_uncertainty)=*q.anchor.get_or_insert((pts,uncertainty));
   let expected=match q.stats.submitted_frames.checked_mul(1_000_000_000).map(|n|n/48_000).and_then(|n|anchor.checked_add(n)){Some(n)=>n,None=>{drop(q);self.fail();return Err(());}};
   let end=match submitted.checked_mul(1_000_000_000).map(|n|n/48_000).and_then(|n|anchor.checked_add(n)){Some(n)=>n,None=>{drop(q);self.fail();return Err(());}};
   let residual=pts.abs_diff(expected);let allowance=initial_uncertainty.saturating_add(uncertainty).saturating_add(20_834).min(2_020_834);
   if residual>allowance||end<=expected {drop(q);self.fail();return Err(());}
   let mut buf=gst::Buffer::from_mut_slice(data);{let b=buf.get_mut().ok_or(())?;b.set_pts(gst::ClockTime::from_nseconds(expected));b.set_duration(gst::ClockTime::from_nseconds(end-expected));}
   if src.current_level_bytes().saturating_add(buf.size() as u64)>48_000 || src.property::<u64>("current-level-buffers")>=32 || src.push_buffer(buf).is_err(){drop(q);self.fail();return Err(());}
   q.last_end=Some(end);q.stats.submitted_frames=submitted;if q.stats.first_running_ns.is_none(){q.stats.first_running_ns=Some(expected.to_string());}q.stats.last_running_ns=Some(expected.to_string());
   let uncertainty=uncertainty.saturating_add(residual);let old=q.stats.max_clock_uncertainty_ns.as_deref().and_then(|v|v.parse::<u64>().ok()).unwrap_or(0);q.stats.max_clock_uncertainty_ns=Some(old.max(uncertainty).to_string());
  }Ok(())
 }
 pub(crate) fn stop_accepting(&self){self.queue.borrow_mut().accepting=false;}
 pub(crate) fn finish(&self,src:&gst_app::AppSrc,now:u64)->Result<(),()> {self.stop_accepting();self.feed(src,now)?;src.end_of_stream().map_err(|_|())?;self.queue.borrow_mut().stats.eos=true;Ok(())}
 pub(crate) fn cancel(&self){self.gate.set(false);let mut q=self.queue.borrow_mut();q.failed=true;q.timing.clear();while q.pcm.pop().is_some(){}}
}

/// Adds one independently selected mono/stereo Opus track to an existing WebM mux.
pub(crate) fn attach(pipeline:&gst::Pipeline,mux:&gst::Element,channels:u32)->Result<gst_app::AppSrc,()> {
 if !(1..=2).contains(&channels){return Err(());}let caps=gst::Caps::builder("audio/x-raw").field("format","S16LE").field("layout","interleaved").field("rate",48_000i32).field("channels",channels as i32).build();
 let src=gst_app::AppSrc::builder().caps(&caps).is_live(true).format(gst::Format::Time).block(false).max_bytes(48_000).build();
 if src.find_property("max-buffers").is_none()||src.find_property("current-level-buffers").is_none(){return Err(());}src.set_property("max-buffers",32u64);
 let convert=gst::ElementFactory::make("audioconvert").build().map_err(|_|())?;let resample=gst::ElementFactory::make("audioresample").build().map_err(|_|())?;let opus=gst::ElementFactory::make("opusenc").build().map_err(|_|())?;let queue=gst::ElementFactory::make("queue").property("max-size-buffers",32u32).property("max-size-bytes",48_000u32).property("max-size-time",250_000_000u64).build().map_err(|_|())?;
 pipeline.add_many([src.upcast_ref(),&convert,&resample,&opus,&queue]).map_err(|_|())?;gst::Element::link_many([src.upcast_ref(),&convert,&resample,&opus,&queue,mux]).map_err(|_|())?;
 // v7 can add a selected track after preview, while the retained pipeline is paused.
 if pipeline.current_state()!=gst::State::Null {for element in [src.upcast_ref::<gst::Element>(),&convert,&resample,&opus,&queue] {element.sync_state_with_parent().map_err(|_|())?;}}
 Ok(src)
}

#[cfg(test)] mod tests {
 use super::*;
 #[test] fn mapped_continuity() {
  gst::init().unwrap();let pipe=gst::Pipeline::default();let src=gst_app::AppSrc::builder().is_live(true).format(gst::Format::Time).max_bytes(48_000).build();let sink=gst::ElementFactory::make("fakesink").build().unwrap();pipe.add_many([src.upcast_ref(),&sink]).unwrap();src.link(&sink).unwrap();pipe.set_state(gst::State::Playing).unwrap();
  for next in [9_000_300u64,16_000_300] {let gate=Rc::new(Cell::new(true));let pcm=AudioPcm::new(gate.clone(),1).unwrap();let c=Calibration::sample(100,1_000_100,102,1_000_000,7,None).unwrap();let c=Calibration::sample(200,1_000_200,202,1_000_000,7,Some(c)).unwrap();*pcm.calibration.borrow_mut()=Some(c);
   for (seq,cycle) in [(1,300),(2,next)] {let timing=Timing{cycle,now:cycle+100,delay_now:cycle+50,delay:0,num:1,den:1,frames:480};pcm.accept(vec![0;960],timing,Some((seq,false)),seq).unwrap();let result=pcm.feed(&src,cycle+100);if seq==1{assert!(result.is_ok());}else{assert!(result.is_err());assert!(!gate.get());}}
  }pipe.set_state(gst::State::Null).unwrap();
 }
 #[test] fn stride_and_measured_stats() {
  assert!(tightly_packed(0,1)&&tightly_packed(2,1)&&tightly_packed(0,2)&&tightly_packed(4,2));
  assert!(!tightly_packed(-1,1)&&!tightly_packed(4,1)&&!tightly_packed(8,2));
  gst::init().unwrap();let pipe=gst::Pipeline::default();let src=gst_app::AppSrc::builder().is_live(true).format(gst::Format::Time).max_bytes(48_000).build();let sink=gst::ElementFactory::make("fakesink").build().unwrap();pipe.add_many([src.upcast_ref(),&sink]).unwrap();src.link(&sink).unwrap();pipe.set_state(gst::State::Playing).unwrap();
  let pcm=AudioPcm::new(Rc::new(Cell::new(true)),1).unwrap();let initial=serde_json::to_value(pcm.stats()).unwrap();assert_eq!(initial["first_running_ns"],serde_json::Value::Null);assert_eq!(initial["sample_rate"],serde_json::Value::Null);assert_eq!(initial["channels"],serde_json::Value::Null);assert_eq!(initial["eos"],false);pcm.mark_negotiated();
  let c=Calibration::sample(100,1_000_100,102,1_000_000,7,None).unwrap();let c=Calibration::sample(200,1_000_200,202,1_000_000,7,Some(c)).unwrap();*pcm.calibration.borrow_mut()=Some(c);
  let timing=Timing{cycle:300,now:400,delay_now:390,delay:-2,num:1,den:1_000_000_000,frames:2};pcm.accept(vec![0;4],timing,Some((1,true)),1).unwrap();pcm.feed(&src,400).unwrap();pcm.finish(&src,400).unwrap();
  let stats=serde_json::to_value(pcm.stats()).unwrap();assert_eq!(stats["stage"],"pcm_appsrc_submission");assert_eq!(stats["assurance"],"software_correlated_estimate");assert_eq!(stats["sample_rate"],48_000);assert_eq!(stats["channels"],1);assert_eq!(stats["queued_frames"],2);assert_eq!(stats["submitted_frames"],2);assert_eq!(stats["first_running_ns"],"301");assert_eq!(stats["last_running_ns"],"301");assert_eq!(stats["max_clock_uncertainty_ns"],"1");assert_eq!(stats["last_graph_delay_ns"],"-2");assert_eq!(stats["discontinuities"],1);assert_eq!(stats["missing_metadata"],0);assert_eq!(stats["eos"],true);
  let missing=AudioPcm::new(Rc::new(Cell::new(true)),1).unwrap();missing.missing();assert_eq!(serde_json::to_value(missing.stats()).unwrap()["missing_metadata"],1);pipe.set_state(gst::State::Null).unwrap();
 }
 #[test] fn synthetic_pcm_video_webm() {
  gst::init().unwrap();
  let pipe=gst::parse::launch("videotestsrc num-buffers=3 ! video/x-raw,width=64,height=48,framerate=15/1 ! videoconvert ! vp8enc ! webmmux name=mux streamable=true ! appsink name=out sync=false").unwrap().downcast::<gst::Pipeline>().unwrap();
  let mux=pipe.by_name("mux").unwrap();let src=attach(&pipe,&mux,1).unwrap();let sink=pipe.by_name("out").unwrap().downcast::<gst_app::AppSink>().unwrap();
  let gate=Rc::new(Cell::new(true));let pcm=AudioPcm::new(gate.clone(),1).unwrap();
  let c=Calibration::sample(100,1_000_100,102,1_000_000,7,None).unwrap();let c=Calibration::sample(200,1_000_200,202,1_000_000,7,Some(c)).unwrap();*pcm.calibration.borrow_mut()=Some(c);
  pipe.set_state(gst::State::Playing).unwrap();
  for i in 0..3 {let cycle=300+i*20_000_000;pcm.accept(vec![0;1920],Timing{cycle,now:cycle+100,delay_now:cycle+50,delay:0,num:1,den:48_000,frames:960},Some((i,false)),i).unwrap();pcm.feed(&src,cycle+100).unwrap();}
  pcm.finish(&src,40_000_500).unwrap();
  let mut data=Vec::new();while let Some(sample)=sink.try_pull_sample(gst::ClockTime::from_seconds(3)){let b=sample.buffer().unwrap();data.extend_from_slice(b.map_readable().unwrap().as_slice());}
  assert!(data.starts_with(&[0x1a,0x45,0xdf,0xa3]));assert!(data.windows(6).any(|w|w==b"A_OPUS"));assert!(data.windows(5).any(|w|w==b"V_VP8"));assert!(sink.is_eos());assert!(gate.get());
  let bad_gate=Rc::new(Cell::new(true));let bad=AudioPcm::new(bad_gate.clone(),1).unwrap();let timing=Timing{cycle:300,now:400,delay_now:390,delay:0,num:1,den:48_000,frames:1};
  bad.accept(vec![0;2],timing,Some((1,false)),1).unwrap();assert!(bad.accept(vec![0;2],timing,Some((1,false)),2).is_err());assert!(!bad_gate.get());
  pipe.set_state(gst::State::Null).unwrap();
 }
}
