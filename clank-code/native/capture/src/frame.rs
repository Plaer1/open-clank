//! A single bounded latest frame from a portal-owned PipeWire remote.
use std::{io::Cursor, os::fd::AsRawFd};
use gstreamer as gst;
use gstreamer_app as gst_app;
use gstreamer_video as gst_video;
use gst::prelude::*;
use gst_video::prelude::*;
use image::{ImageBuffer, ImageFormat, Rgba};

pub struct Frames { pipeline: gst::Pipeline, _fd: std::os::fd::OwnedFd, sink: gst_app::AppSink, last_size: Option<(u32,u32)>, geometry: u64 }
pub struct Frame { pub png: Vec<u8>, pub width: u32, pub height: u32, pub pts_ns: Option<u64>, pub geometry: u64 }
impl Frames {
    pub fn new(fd: &std::os::fd::OwnedFd, node: u32, loss: tokio::sync::mpsc::Sender<()>) -> Result<Self, ()> {
        gst::init().map_err(|_| ())?;
        let owned_fd = fd.try_clone().map_err(|_| ())?;
        let source = gst::ElementFactory::make("pipewiresrc").property("fd", owned_fd.as_raw_fd()).property("path", node.to_string()).build().map_err(|_| ())?;
        // Without explicit disconnect signalling, a removed source can look idle forever.
        if source.find_property("on-disconnect").is_none() { return Err(()); }
        source.set_property_from_str("on-disconnect", "error");
        Self::with_source(owned_fd, source, loss)
    }
    /// The transferred descriptor is a fresh portal remote used by this one
    /// consumer only; the serial is the control owner's selected object.
    pub fn new_control(owned_fd: std::os::fd::OwnedFd, serial: u64, loss: tokio::sync::mpsc::Sender<()>) -> Result<Self, ()> {
        if serial == 0 { return Err(()); }
        gst::init().map_err(|_| ())?;
        let source = gst::ElementFactory::make("pipewiresrc").property("fd", owned_fd.as_raw_fd()).build().map_err(|_| ())?;
        if ["target-object", "stream-properties", "on-disconnect"].iter().any(|key| source.find_property(key).is_none()) { return Err(()); }
        source.set_property("target-object", serial.to_string());
        source.set_property_from_str("on-disconnect", "error");
        let props = gst::Structure::builder("props").field("node.dont-reconnect", "true").field("node.dont-fallback", "true").field("node.dont-move", "true").build();
        source.set_property("stream-properties", props);
        Self::with_source(owned_fd, source, loss)
    }
    fn with_source(owned_fd: std::os::fd::OwnedFd, source: gst::Element, loss: tokio::sync::mpsc::Sender<()>) -> Result<Self, ()> {
        let convert = gst::ElementFactory::make("videoconvert").build().map_err(|_| ())?;
        let sink = gst_app::AppSink::builder().caps(&gst::Caps::builder("video/x-raw").field("format", "RGBA")
            .field("width", gst::IntRange::new(1i32, 4096i32)).field("height", gst::IntRange::new(1i32, 4096i32)).build()).build();
        sink.set_max_buffers(1); sink.set_drop(true); sink.set_wait_on_eos(false);
        let pipeline = gst::Pipeline::default();
        let bus = pipeline.bus().ok_or(())?;
        bus.set_sync_handler(move |_, message| {
            if matches!(message.view(), gst::MessageView::Error(_) | gst::MessageView::Eos(_)) { let _ = loss.try_send(()); }
            gst::BusSyncReply::Drop
        });
        pipeline.add_many([&source, &convert, sink.upcast_ref()]).map_err(|_| ())?;
        gst::Element::link_many([&source, &convert, sink.upcast_ref()]).map_err(|_| ())?;
        if pipeline.set_state(gst::State::Playing).is_err() {
            let _ = pipeline.set_state(gst::State::Null);
            return Err(());
        }
        Ok(Self {pipeline, _fd:owned_fd, sink, last_size:None, geometry:0})
    }
    pub fn latest(&mut self) -> Result<Option<Frame>, ()> {
        if let Some(bus) = self.pipeline.bus() {
            while let Some(message) = bus.pop() {
                if matches!(message.view(), gst::MessageView::Error(_) | gst::MessageView::Eos(_)) { return Err(()); }
            }
        }
        let Some(sample) = self.sink.try_pull_sample(gst::ClockTime::from_seconds(1)) else { return Ok(None) };
        let info = gst_video::VideoInfo::from_caps(sample.caps().ok_or(())?).map_err(|_| ())?;
        let (width,height)=(info.width(),info.height());
        if info.format()!=gst_video::VideoFormat::Rgba || width==0 || height==0 || width>4096 || height>4096 || u64::from(width)*u64::from(height)>16_000_000 { return Err(()); }
        let buffer=sample.buffer().ok_or(())?; let pts_ns=buffer.pts().map(|v|v.nseconds());
        let mapped=gst_video::VideoFrameRef::from_buffer_ref_readable(buffer, &info).map_err(|_| ())?;
        let stride=*mapped.plane_stride().first().ok_or(())?;
        let row=usize::try_from(width).map_err(|_| ())?.checked_mul(4).ok_or(())?;
        let stride=usize::try_from(stride).map_err(|_| ())?; if stride<row { return Err(()); }
        let data=mapped.plane_data(0).map_err(|_| ())?;
        let mut packed=Vec::with_capacity(row.checked_mul(height as usize).ok_or(())?);
        for y in 0..height as usize { let start=y.checked_mul(stride).ok_or(())?; packed.extend_from_slice(data.get(start..start.checked_add(row).ok_or(())?).ok_or(())?); }
        let image: ImageBuffer<Rgba<u8>,Vec<u8>>=ImageBuffer::from_raw(width,height,packed).ok_or(())?;
        let mut encoded=Cursor::new(Vec::new()); image.write_to(&mut encoded,ImageFormat::Png).map_err(|_| ())?;
        let png=encoded.into_inner(); if png.len()>64*1024*1024 {return Err(());}
        if self.last_size!=Some((width,height)) { self.geometry=self.geometry.checked_add(1).ok_or(())?;self.last_size=Some((width,height)); }
        Ok(Some(Frame {png,width,height,pts_ns,geometry:self.geometry}))
    }
}
impl Drop for Frames { fn drop(&mut self) { let _=self.pipeline.set_state(gst::State::Null); } }
