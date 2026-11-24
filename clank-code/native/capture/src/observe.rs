//! Bounded, per-pad observations; these do not measure upstream/compositor loss.
use std::sync::{Arc, Mutex};
use gstreamer as gst;
use gst::prelude::*;
use serde_json::{json, Value};

#[derive(Default)]
struct Stage {
 segment: Option<gst::FormattedSegment<gst::ClockTime>>,
 buffers:u64, first_pts:Option<u64>, last_pts:Option<u64>,
 first_running:Option<u64>, last_running:Option<u64>, missing_pts:u64, missing_running:u64,
 caps:Option<gst::Caps>, pixels:Option<(i32,i32)>, caps_changed:bool,
}
impl Stage {
 fn view(&self)->Value {json!({"buffers":self.buffers,"first_pts_ns":self.first_pts.map(|x|x.to_string()),"last_pts_ns":self.last_pts.map(|x|x.to_string()),
  "first_running_ns":self.first_running.map(|x|x.to_string()),"last_running_ns":self.last_running.map(|x|x.to_string()),
  "missing_pts":self.missing_pts,"missing_running":self.missing_running})}
}
#[derive(Default)]
pub(crate) struct Observer { source:Arc<Mutex<Stage>>, rate_output:Arc<Mutex<Stage>>, encoded:Arc<Mutex<Stage>>, output:Arc<Mutex<Stage>> }
impl Observer {
 pub(crate) fn attach(&self,source:&gst::Element,rate:&gst::Element,encoder:&gst::Element,output:&gst::Element)->Result<(),()> {
  Self::attach_pad(source,"src",self.source.clone(),true)?;
  Self::attach_pad(rate,"src",self.rate_output.clone(),false)?;
  Self::attach_pad(encoder,"src",self.encoded.clone(),false)?;
  Self::attach_pad(output,"src",self.output.clone(),true)
 }
 fn attach_pad(element:&gst::Element,name:&str,state:Arc<Mutex<Stage>>,caps:bool)->Result<(),()> {
  element.static_pad(name).ok_or(())?.add_probe(gst::PadProbeType::BUFFER|gst::PadProbeType::EVENT_DOWNSTREAM,move|_,probe|{
   let Ok(mut s)=state.lock() else{return gst::PadProbeReturn::Ok;};
   if let Some(event)=probe.event(){match event.view(){
    gst::EventView::Segment(v)=>s.segment=v.segment().downcast_ref::<gst::ClockTime>().cloned(),
    gst::EventView::Caps(v) if caps=>{let new=v.caps();if s.caps.as_ref().is_some_and(|old|old!=new){s.caps_changed=true;}
     s.caps=Some(new.to_owned());s.pixels=new.structure(0).and_then(|v|Some((v.get::<i32>("width").ok()?,v.get::<i32>("height").ok()?))).filter(|(w,h)|*w>0&&*h>0&&*w<=65536&&*h<=65536);},
    _=>{}
   }}
   if let Some(buffer)=probe.buffer(){s.buffers=s.buffers.saturating_add(1).min(9007199254740991);if let Some(pts)=buffer.pts(){let p=pts.nseconds();s.first_pts.get_or_insert(p);s.last_pts=Some(p);
     if let Some(t)=s.segment.as_ref().and_then(|x|x.to_running_time(pts)){let n=t.nseconds();s.first_running.get_or_insert(n);s.last_running=Some(n);}else{s.missing_running=s.missing_running.saturating_add(1).min(9007199254740991);}
    }else{s.missing_pts=s.missing_pts.saturating_add(1).min(9007199254740991);s.missing_running=s.missing_running.saturating_add(1).min(9007199254740991);}}
   gst::PadProbeReturn::Ok
  }).ok_or(())?;Ok(())
 }
 /// Called only while the v7 pipeline is paused and flushed, before the new epoch.
 pub(crate) fn reset_epoch(&self)->Result<(),()> {
  for state in [&self.source,&self.rate_output,&self.encoded,&self.output] {
   let mut s=state.lock().map_err(|_|())?;let caps=s.caps.take();let pixels=s.pixels;*s=Stage::default();s.caps=caps;s.pixels=pixels;
  }Ok(())
 }
 pub(crate) fn snapshot(&self,rate:&gst::Element,output:Option<(i32,i32)>)->Value {
  let source=self.source.lock().unwrap_or_else(|e|e.into_inner());
  let out=self.output.lock().unwrap_or_else(|e|e.into_inner());
  let post=self.rate_output.lock().unwrap_or_else(|e|e.into_inner());
  let encoded=self.encoded.lock().unwrap_or_else(|e|e.into_inner());
  json!({"source_pixels":source.pixels,"output_pixels":output,"caps_changed":source.caps_changed||out.caps_changed,
   "source":source.view(),"rate_output":post.view(),"encoded":encoded.view(),
   "videorate":{"in":rate.property::<u64>("in").min(9007199254740991),"out":rate.property::<u64>("out").min(9007199254740991),"drop":rate.property::<u64>("drop").min(9007199254740991),"duplicate":rate.property::<u64>("duplicate").min(9007199254740991)}})
 }
}

#[cfg(test)]
mod tests {
 use super::*;
 #[test]
 fn synthetic_observer_pipeline() {
  gst::init().unwrap();
  let pipe=gst::parse::launch("videotestsrc name=source num-buffers=6 ! video/x-raw,width=64,height=48,framerate=30/1 ! videorate name=rate drop-only=true ! video/x-raw,framerate=15/1 ! capsfilter name=output ! vp8enc name=encoder ! fakesink").unwrap().downcast::<gst::Pipeline>().unwrap();
  let source=pipe.by_name("source").unwrap();let rate=pipe.by_name("rate").unwrap();let output=pipe.by_name("output").unwrap();let encoder=pipe.by_name("encoder").unwrap();
  let observer=Observer::default();observer.attach(&source,&rate,&encoder,&output).unwrap();
  pipe.set_state(gst::State::Playing).unwrap();
  let msg=pipe.bus().unwrap().timed_pop_filtered(gst::ClockTime::from_seconds(5),&[gst::MessageType::Eos,gst::MessageType::Error]).unwrap();
  assert!(matches!(msg.view(),gst::MessageView::Eos(_)));
  let result=observer.snapshot(&rate,Some((64,48)));
  assert_eq!(result["source_pixels"],json!([64,48]));assert_eq!(result["output_pixels"],json!([64,48]));
  assert_eq!(result["source"]["buffers"],6);assert!(result["encoded"]["buffers"].as_u64().unwrap()>0);
  assert!(result["source"]["first_pts_ns"].as_str().is_some());assert!(result["source"]["first_running_ns"].as_str().is_some());
  assert_eq!(result["videorate"]["in"],6);assert!(result["videorate"]["drop"].as_u64().unwrap()>0);
  pipe.set_state(gst::State::Null).unwrap();
 }
}
