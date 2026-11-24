//! Experimental v7 same-source video branch; not registered or advertised.
//! The caller retains the source, pipeline and portal authority throughout.
use std::{io::Cursor,sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}}};
use gstreamer as gst;
use gstreamer_app as gst_app;
use gstreamer_video as gst_video;
use gst::prelude::*;
use gst_video::prelude::*;
use image::{ImageBuffer,ImageFormat,Rgba};
use crate::record_crop::Rect;

fn valid_raw_caps(caps:&gst::CapsRef)->bool {
 let Ok(info)=gst_video::VideoInfo::from_caps(caps) else{return false;};
 let (w,h)=(info.width(),info.height());
 caps.structure(0).is_some_and(|s|s.name()=="video/x-raw") && info.format()!=gst_video::VideoFormat::Unknown
  && info.par()==gst::Fraction::new(1,1) && w>0 && h>0 && w<=16384 && h<=16384 && u64::from(w)*u64::from(h)<=16_000_000
}
pub(crate) struct CropPreview {
 pub(crate) source:gst::Element,
 pub(crate) output:gst::Element,
 preview:gst_app::AppSink,
 preview_gate:gst::Element,
 gate:gst::Element,
 crop:gst::Element,
 basis:Option<gst::Caps>,
 witnessed:Arc<Mutex<Option<gst::Caps>>>,
 drift:Arc<AtomicBool>,
 segment:Arc<AtomicBool>,
 armed:Arc<AtomicBool>,
}
pub(crate) struct Preview {pub(crate) png:Vec<u8>,pub(crate) source:(u32,u32),pub(crate) pixels:(u32,u32)}
impl CropPreview {
 /// Add one source and its bounded preview and blocked output branches to an existing pipeline.
 pub(crate) fn attach(pipe:&gst::Pipeline,source:gst::Element)->Result<Self,()> {
  let tee=gst::ElementFactory::make("tee").build().map_err(|_|())?;
  let pq=gst::ElementFactory::make("queue").property("max-size-buffers",1u32).property("max-size-bytes",0u32).property("max-size-time",0u64).property_from_str("leaky","downstream").build().map_err(|_|())?;
  let preview_gate=gst::ElementFactory::make("valve").property("drop",false).build().map_err(|_|())?;
  let convert=gst::ElementFactory::make("videoconvert").build().map_err(|_|())?;
  let scale=gst::ElementFactory::make("videoscale").build().map_err(|_|())?;
  let preview=gst_app::AppSink::builder().caps(&gst::Caps::builder("video/x-raw").field("format","RGBA").field("width",gst::IntRange::new(1,960)).field("height",gst::IntRange::new(1,540)).build()).build();
  preview.set_property("async",false);preview.set_max_buffers(1);preview.set_drop(true);preview.set_wait_on_eos(false);preview.set_sync(false);
  let oq=gst::ElementFactory::make("queue").property("max-size-buffers",1u32).property("max-size-bytes",0u32).property("max-size-time",0u64).property_from_str("leaky","downstream").build().map_err(|_|())?;
  let gate=gst::ElementFactory::make("valve").property("drop",true).build().map_err(|_|())?;
  let crop=gst::ElementFactory::make("videocrop").build().map_err(|_|())?;
  let witnessed:Arc<Mutex<Option<gst::Caps>>>=Arc::new(Mutex::new(None));let drift=Arc::new(AtomicBool::new(false));
  let witness=witnessed.clone();let changed=drift.clone();
  source.static_pad("src").ok_or(())?.add_probe(gst::PadProbeType::EVENT_DOWNSTREAM|gst::PadProbeType::BUFFER,move|_,info|{
   if let Some(event)=info.event(){if let gst::EventView::Caps(value)=event.view(){if !valid_raw_caps(value.caps()){changed.store(true,Ordering::Release);return gst::PadProbeReturn::Drop;}let Ok(mut old)=witness.lock() else{changed.store(true,Ordering::Release);return gst::PadProbeReturn::Drop;};if old.as_ref().is_some_and(|x|x!=value.caps()){changed.store(true,Ordering::Release);}else if old.is_none(){*old=Some(value.caps().to_owned());}}}
   if changed.load(Ordering::Acquire){gst::PadProbeReturn::Drop}else{gst::PadProbeReturn::Ok}
  }).ok_or(())?;
  let segment=Arc::new(AtomicBool::new(false));let armed=Arc::new(AtomicBool::new(false));let seen=segment.clone();let enabled=armed.clone();let failed=drift.clone();
  crop.static_pad("src").ok_or(())?.add_probe(gst::PadProbeType::EVENT_DOWNSTREAM|gst::PadProbeType::BUFFER,move|_,info|{
   if !enabled.load(Ordering::Acquire){return gst::PadProbeReturn::Ok;}
   if let Some(event)=info.event(){if let gst::EventView::Segment(value)=event.view(){if value.segment().downcast_ref::<gst::ClockTime>().is_some_and(|v|v.rate()==1.0 && v.applied_rate()==1.0){seen.store(true,Ordering::Release);}else{failed.store(true,Ordering::Release);return gst::PadProbeReturn::Drop;}}}
   if info.buffer().is_some()&&!seen.load(Ordering::Acquire){failed.store(true,Ordering::Release);return gst::PadProbeReturn::Drop;}
   gst::PadProbeReturn::Ok
  }).ok_or(())?;
  pipe.add_many([&source,&tee,&pq,&preview_gate,&convert,&scale,preview.upcast_ref(),&oq,&gate,&crop]).map_err(|_|())?;
  gst::Element::link_many([&source,&tee]).map_err(|_|())?;
  gst::Element::link_many([&tee,&pq,&preview_gate,&convert,&scale,preview.upcast_ref()]).map_err(|_|())?;
  gst::Element::link_many([&tee,&oq,&gate,&crop]).map_err(|_|())?;
  Ok(Self{source,output:crop.clone(),preview,preview_gate,gate,crop,basis:None,witnessed,drift,segment,armed})
 }
 /// Read a single sample; only an explicitly square, unrotated raw basis is admitted.
 pub(crate) fn sample(&mut self)->Result<Option<Preview>,()> {
  if self.basis.is_some(){return Err(());}
  let Some(sample)=self.preview.try_pull_sample(gst::ClockTime::from_seconds(1)) else{return Ok(None)};
  let caps=self.source.static_pad("src").and_then(|p|p.current_caps()).ok_or(())?;
  let st=caps.structure(0).ok_or(())?;
  if !valid_raw_caps(&caps) {return Err(());}
  // Both branches share this raw basis; no orientation transform is inserted. Physical orientation is unknown.
  if self.drift.load(Ordering::Acquire)||self.witnessed.lock().map_err(|_|())?.as_ref()!=Some(&caps){return Err(());}
  let w=u32::try_from(st.get::<i32>("width").map_err(|_|())?).map_err(|_|())?;
  let h=u32::try_from(st.get::<i32>("height").map_err(|_|())?).map_err(|_|())?;
  if w==0||h==0||w>16384||h>16384||u64::from(w)*u64::from(h)>16_000_000{return Err(());}
  let info=gst_video::VideoInfo::from_caps(sample.caps().ok_or(())?).map_err(|_|())?;
  let (pw,ph)=(info.width(),info.height());if info.format()!=gst_video::VideoFormat::Rgba||pw==0||ph==0||pw>960||ph>540||(u64::from(w)*u64::from(ph)).abs_diff(u64::from(h)*u64::from(pw))>u64::from(w.max(h)){return Err(());}
  let frame=gst_video::VideoFrameRef::from_buffer_ref_readable(sample.buffer().ok_or(())?,&info).map_err(|_|())?;
  let stride=usize::try_from(*frame.plane_stride().first().ok_or(())?).map_err(|_|())?;let row=usize::try_from(pw).map_err(|_|())?.checked_mul(4).ok_or(())?;
  if stride<row{return Err(());}let data=frame.plane_data(0).map_err(|_|())?;let mut pixels=Vec::with_capacity(row.checked_mul(ph as usize).ok_or(())?);
  for y in 0..ph as usize {let a=y.checked_mul(stride).ok_or(())?;pixels.extend_from_slice(data.get(a..a.checked_add(row).ok_or(())?).ok_or(())?);}
  let image:ImageBuffer<Rgba<u8>,Vec<u8>>=ImageBuffer::from_raw(pw,ph,pixels).ok_or(())?;let mut out=Cursor::new(Vec::new());image.write_to(&mut out,ImageFormat::Png).map_err(|_|())?;
  let png=out.into_inner();if png.len()>2*1024*1024{return Err(());}self.preview_gate.set_property("drop",true);self.basis=Some(caps);Ok(Some(Preview{png,source:(w,h),pixels:(pw,ph)}))
 }
 pub(crate) fn basis_current(&self)->bool {!self.drift.load(Ordering::Acquire)&&self.basis.as_ref().is_some_and(|basis|self.source.static_pad("src").and_then(|p|p.current_caps()).as_ref()==Some(basis))}
 pub(crate) fn set_crop(&self,r:&Rect)->Result<(),()> {
  let caps=self.basis.as_ref().ok_or(())?;let st=caps.structure(0).ok_or(())?;
  let w=u32::try_from(st.get::<i32>("width").map_err(|_|())?).map_err(|_|())?;let h=u32::try_from(st.get::<i32>("height").map_err(|_|())?).map_err(|_|())?;
  let right=w.checked_sub(r.x.checked_add(r.width).ok_or(())?).ok_or(())?;let bottom=h.checked_sub(r.y.checked_add(r.height).ok_or(())?).ok_or(())?;
  if r.width==0||r.height==0||!self.basis_current(){return Err(());}self.crop.set_property("left",r.x as i32);self.crop.set_property("top",r.y as i32);self.crop.set_property("right",right as i32);self.crop.set_property("bottom",bottom as i32);Ok(())
 }
 pub(crate) fn close_preview(&self) {self.preview_gate.set_property("drop",true);}
 /// Re-enable the bounded discarded preview sink only for pipeline EOS propagation.
 pub(crate) fn prepare_eos(&self) {self.preview_gate.set_property("drop",false);}
 /// Establish a new clock epoch without replacing the source or portal stream.
 /// The caller still must prove fresh downstream segment/buffers before admitting recording.
 pub(crate) fn reset_epoch(&self,pipe:&gst::Pipeline,r:&Rect,reset_observers:impl FnOnce())->Result<(),()> {
  self.gate.set_property("drop",true);self.close_preview();
  if !self.basis_current(){return Err(());}let before=pipe.base_time().ok_or(())?;
  pipe.set_state(gst::State::Paused).map_err(|_|())?;
  let (result,state,_)=pipe.state(gst::ClockTime::from_seconds(1));if result.is_err()||state!=gst::State::Paused{return Err(());}
  if !pipe.send_event(gst::event::FlushStart::new())||!pipe.send_event(gst::event::FlushStop::new(true)){return Err(());}
  self.set_crop(r)?;reset_observers();self.segment.store(false,Ordering::Release);self.armed.store(true,Ordering::Release);pipe.set_start_time(gst::ClockTime::ZERO);
  pipe.set_state(gst::State::Playing).map_err(|_|())?;
  let (result,state,_)=pipe.state(gst::ClockTime::from_seconds(1));if result.is_err()||state!=gst::State::Playing||!self.basis_current(){return Err(());}
  if pipe.base_time().is_none_or(|after|after<=before){return Err(());}Ok(())
 }
 pub(crate) fn fresh_segment(&self)->bool {self.armed.load(Ordering::Acquire)&&self.segment.load(Ordering::Acquire)&&!self.drift.load(Ordering::Acquire)}
 pub(crate) fn open_gate(&self)->Result<(),()> {if !self.basis_current(){return Err(());}self.gate.set_property("drop",false);Ok(())}
}
