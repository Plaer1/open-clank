//! Same-core inactive streams and owned links. Not a public capture profile.
use super::*;
use std::cell::Cell;
use pw::spa::{self, pod::Pod};
use gstreamer as gst;
use gstreamer_app as gst_app;
use gst::prelude::*;

fn request_header(stream:&pw::stream::Stream)->Result<(),()> {
 let meta=spa::pod::Object{type_:spa::utils::SpaTypes::ObjectParamMeta.as_raw(),id:spa::param::ParamType::Meta.as_raw(),properties:vec![
  spa::pod::Property::new(spa::sys::SPA_PARAM_META_type,spa::pod::Value::Id(spa::utils::Id(spa::sys::SPA_META_Header))),
  spa::pod::Property::new(spa::sys::SPA_PARAM_META_size,spa::pod::Value::Int(std::mem::size_of::<spa::sys::spa_meta_header>() as i32))]};
 let bytes=spa::pod::serialize::PodSerializer::serialize(std::io::Cursor::new(Vec::new()),&spa::pod::Value::Object(meta)).map_err(|_|())?.0.into_inner();
 let mut params=[Pod::from_bytes(&bytes).ok_or(())?];stream.update_params(&mut params).map_err(|_|())
}
#[derive(Default)]
struct Status {format_ok:bool, failed:bool}
#[derive(Default)]
struct LinkStatus {id:Option<u32>,ready:bool,failed:bool}

pub(super) struct Guard {pub(super) source:Binding,pub(super) inputs:Vec<(u32,u64,String)>,pub(super) node:u32,pub(super) node_serial:u64,pub(super) links:Vec<(u32,u64)>,pub(super) gate:Rc<Cell<bool>>}

pub(crate) struct Prepared {
 stream:pw::stream::StreamRc,
 _stream_listener:pw::stream::StreamListener<()>,
 links:Vec<pw::link::Link>, _listeners:Vec<pw::link::LinkListener>,
 link_state:Vec<Rc<RefCell<LinkStatus>>>, status:Rc<RefCell<Status>>,
 source:Binding, inputs:Vec<(u32,u64,String)>, node:u32, node_serial:u64, gate:Rc<Cell<bool>>, pcm:Rc<crate::audio_pcm::AudioPcm>, registry_state:Rc<RefCell<State>>, closed:Cell<bool>,
}
impl Drop for Prepared {fn drop(&mut self){self.closed.set(true);self.pcm.cancel();self.gate.set(false);self.registry_state.borrow_mut().guards.remove(&self.node);let _=self.stream.set_active(false);let _=self.stream.disconnect();}}
impl RegistryOwner {
 /// Capture authority is rechecked throughout preparation. No default target is selected.
 pub(crate) fn prepare_frozen(&self,source:Binding,timeout:Duration,current:&dyn Fn()->bool)->Result<Prepared,()> {
  if !unsafe{pw::sys::pw_check_library_version(1,0,5)} || timeout.is_zero() || timeout>Duration::from_secs(2) || !current(){return Err(());}
  self.poll()?;if !self.state.borrow().binding_current(&source){return Err(());}
  let stream=pw::stream::StreamRc::new(self.core.clone(),"openclank-owned-audio",pw::properties::properties!{
   "media.type"=>"Audio", "media.category"=>"Capture", "media.role"=>"Screen", "node.autoconnect"=>"false", "node.dont-reconnect"=>"true", "node.dont-move"=>"true"
  }).map_err(|_|())?;
  let gate=Rc::new(Cell::new(false));let format_gate=gate.clone();let stream_gate=gate.clone();
  let pcm=crate::audio_pcm::AudioPcm::new(gate.clone(),source.ports.len() as u32)?;let capture=pcm.clone();let negotiated=pcm.clone();
  let status=Rc::new(RefCell::new(Status::default()));let f=status.clone();let g=status.clone();let count=source.ports.len() as u32;
  let mut positions=[0u32;spa::sys::SPA_AUDIO_MAX_CHANNELS as usize];
  if count==1 {positions[0]=spa::sys::SPA_AUDIO_CHANNEL_MONO;}else{positions[0]=spa::sys::SPA_AUDIO_CHANNEL_FL;positions[1]=spa::sys::SPA_AUDIO_CHANNEL_FR;}
  let listener=stream.add_local_listener::<()>().param_changed(move|stream,_,id,param|{
   if id!=spa::param::ParamType::Format.as_raw(){return;}let mut state=f.borrow_mut();
   let Some(param)=param else{state.format_ok=false;format_gate.set(false);return;};let mut info=spa::param::audio::AudioInfoRaw::new();
   state.format_ok=info.parse(param).is_ok() && info.format()==spa::param::audio::AudioFormat::S16LE && info.rate()==48_000 && info.channels()==count && info.position()[..count as usize]==positions[..count as usize];
   if state.format_ok && request_header(stream).is_ok(){negotiated.mark_negotiated();}else{state.format_ok=false;state.failed=true;format_gate.set(false);}
  }).process(move|stream,_|capture.process(stream)).state_changed(move|_,_,_,new|{if matches!(new,pw::stream::StreamState::Error(_)|pw::stream::StreamState::Unconnected){g.borrow_mut().failed=true;stream_gate.set(false);}}).register().map_err(|_|())?;
  let mut format=spa::param::audio::AudioInfoRaw::new();format.set_format(spa::param::audio::AudioFormat::S16LE);format.set_rate(48_000);format.set_channels(count);format.set_position(positions);
  let obj=spa::pod::Object{type_:spa::utils::SpaTypes::ObjectParamFormat.as_raw(),id:spa::param::ParamType::EnumFormat.as_raw(),properties:format.into()};
  let bytes=spa::pod::serialize::PodSerializer::serialize(std::io::Cursor::new(Vec::new()),&spa::pod::Value::Object(obj)).map_err(|_|())?.0.into_inner();
  let mut params=[Pod::from_bytes(&bytes).ok_or(())?];
  stream.connect(spa::utils::Direction::Input,None,pw::stream::StreamFlags::INACTIVE|pw::stream::StreamFlags::MAP_BUFFERS,&mut params).map_err(|_|())?;
  let end=Instant::now()+timeout;
  let (node,node_serial,inputs)=loop {
   if !current() || self.state.borrow().lost || status.borrow().failed || Instant::now()>=end{return Err(());}
   self.step(end)?;let id=stream.node_id();let state=self.state.borrow();if !state.binding_current(&source){return Err(());}
   let candidates:Vec<_>=state.ports.iter().filter(|(_,p)|p.input&&p.parent==id).map(|(&id,p)|(id,p.serial,p.channel.clone())).collect();
   if let Some(n)=state.nodes.get(&id){if n.kind=="owned_stream" && candidates.len()==source.ports.len() && source.ports.iter().all(|s|candidates.iter().filter(|p|p.2==s.2).count()==1){break (id,n.serial,candidates);}}
  };
  let mut links=Vec::new();let mut listeners=Vec::new();let mut link_state=Vec::new();
  for (out_id,_,channel) in &source.ports {
   if !current()||!self.state.borrow().binding_current(&source){return Err(());}let input=inputs.iter().find(|p|p.2==*channel).ok_or(())?;
   let link=self.core.create_object::<pw::link::Link>("link-factory",&pw::properties::properties!{
    "link.output.node"=>source.node_id.to_string(),"link.output.port"=>out_id.to_string(),"link.input.node"=>node.to_string(),"link.input.port"=>input.0.to_string(),"object.linger"=>"false"
   }).map_err(|_|())?;
   let observed=Rc::new(RefCell::new(LinkStatus::default()));let update=observed.clone();let link_gate=gate.clone();let expected=(source.node_id,*out_id,node,input.0);
   let listener=link.add_listener_local().info(move|info|{let mut s=update.borrow_mut();let endpoints=(info.output_node_id(),info.output_port_id(),info.input_node_id(),info.input_port_id());
    s.id=Some(info.id());s.ready=endpoints==expected&&matches!(info.state(),pw::link::LinkState::Paused|pw::link::LinkState::Active);
    if !s.ready {link_gate.set(false);}if endpoints!=expected||matches!(info.state(),pw::link::LinkState::Error(_)|pw::link::LinkState::Unlinked){s.failed=true;}
   }).register();links.push(link);listeners.push(listener);link_state.push(observed);
  }
  let prepared=Prepared{stream,_stream_listener:listener,links,_listeners:listeners,link_state,status,source,inputs,node,node_serial,gate,pcm,registry_state:self.state.clone(),closed:Cell::new(false)};
  // Link identities must be present before transport is started. PipeWire starts
  // link negotiation only when linked nodes are active; the application PCM gate
  // remains closed throughout this provisional stream activation.
  loop {if !current()||Instant::now()>=end{return Err(());}self.step(end)?;if prepared.failed(self){return Err(());}if prepared.linked(self){break;}}
  if !current()||prepared.gate.get(){return Err(());}prepared.stream.set_active(true).map_err(|_|())?;
  loop {if !current()||Instant::now()>=end{return Err(());}self.step(end)?;if prepared.failed(self){return Err(());}if prepared.ready(self){return Ok(prepared);}}
 }
 fn step(&self,end:Instant)->Result<(),()> {if self.main.loop_().iterate(pw::loop_::Timeout::Finite(end.saturating_duration_since(Instant::now()).min(Duration::from_millis(20))))<0{self.state.borrow_mut().lose();Err(())}else{Ok(())}}
}
impl Prepared {
 fn failed(&self,owner:&RegistryOwner)->bool {!Rc::ptr_eq(&self.registry_state,&owner.state)||self.closed.get()||self.status.borrow().failed||self.link_state.iter().any(|x|x.borrow().failed)||!owner.state.borrow().binding_current(&self.source)}
 fn linked(&self,owner:&RegistryOwner)->bool {
  if self.failed(owner){return false;}let s=owner.state.borrow();let own:Vec<_>=self.link_state.iter().filter_map(|x|x.borrow().id).collect();
  graph_matches(&s,&self.source,&self.inputs,self.node,self.node_serial,&own,self.links.len())
 }
 fn ready(&self,owner:&RegistryOwner)->bool {
  self.linked(owner)&&self.status.borrow().format_ok&&self.link_state.iter().all(|x|x.borrow().ready)
 }
 pub(crate) fn activate(&self,owner:&RegistryOwner,current:&dyn Fn()->bool)->Result<(),()> {if !Rc::ptr_eq(&self.registry_state,&owner.state){return Err(());}owner.poll()?;if !current()||!self.ready(owner){return Err(());}
  let ids:Vec<_>=self.link_state.iter().filter_map(|x|x.borrow().id).collect();let links={let s=owner.state.borrow();ids.iter().map(|id|s.links.get(id).map(|l|(*id,l.serial))).collect::<Option<Vec<_>>>().ok_or(())?};
  let guard=Guard{source:self.source.clone(),inputs:self.inputs.clone(),node:self.node,node_serial:self.node_serial,links,gate:self.gate.clone()};
  if !guard_matches(&owner.state.borrow(),&guard){return Err(());}owner.state.borrow_mut().guards.insert(self.node,guard);
  if self.pcm.drain_before_gate(&self.stream).is_err(){owner.state.borrow_mut().guards.remove(&self.node);let _=self.stream.set_active(false);return Err(());}
  self.gate.set(true);if self.stream.set_active(true).is_err()||!self.gate.get()||!current(){self.gate.set(false);owner.state.borrow_mut().guards.remove(&self.node);let _=self.stream.set_active(false);return Err(());}Ok(())}
 pub(crate) fn check(&self,owner:&RegistryOwner,current:&dyn Fn()->bool)->Result<(),()> {if !Rc::ptr_eq(&self.registry_state,&owner.state)||owner.poll().is_err(){self.closed.set(true);self.gate.set(false);let _=self.stream.set_active(false);return Err(());}if !current()||!self.gate.get()||!self.ready(owner){self.closed.set(true);self.gate.set(false);let _=self.stream.set_active(false);return Err(());}Ok(())}
 pub(crate) fn gate_open(&self)->bool {self.gate.get()&&!self.closed.get()}
 pub(crate) fn stats(&self)->crate::audio_pcm::TrackStats {self.pcm.stats()}
 pub(crate) fn calibrate(&self,pipeline:&gst::Pipeline)->Result<(),()> {
  let result=(||{let clock=pipeline.clock().ok_or(())?;let base=pipeline.base_time().ok_or(())?.nseconds();self.pcm.calibrate(&self.stream,&clock,base)})();
  if result.is_err(){self.closed.set(true);self.cancel_audio();}result
 }
 pub(crate) fn feed(&self,owner:&RegistryOwner,pipeline:&gst::Pipeline,src:&gst_app::AppSrc,current:&dyn Fn()->bool)->Result<(),()> {
  self.check(owner,current)?;let result=(||{self.calibrate(pipeline)?;let now=unsafe{pw::sys::pw_stream_get_nsec(self.stream.as_raw_ptr())};self.pcm.feed(src,now)})();
  if result.is_err(){self.closed.set(true);self.cancel_audio();}result
 }
 pub(crate) fn stop_audio(&self,owner:&RegistryOwner,pipeline:&gst::Pipeline,src:&gst_app::AppSrc,current:&dyn Fn()->bool)->Result<(),()> {
  self.check(owner,current)?;self.pcm.stop_accepting();let result=(||{self.stream.set_active(false).map_err(|_|())?;self.calibrate(pipeline)?;let now=unsafe{pw::sys::pw_stream_get_nsec(self.stream.as_raw_ptr())};self.pcm.finish(src,now)})();
  self.closed.set(true);self.gate.set(false);if result.is_err(){self.cancel_audio();}result
 }
 pub(crate) fn cancel_audio(&self){self.pcm.cancel();let _=self.stream.set_active(false);}
}

pub(super) fn guard_matches(s:&State,g:&Guard)->bool {let ids:Vec<_>=g.links.iter().map(|x|x.0).collect();g.links.iter().all(|(id,serial)|s.links.get(id).is_some_and(|l|l.serial==*serial))&&graph_matches(s,&g.source,&g.inputs,g.node,g.node_serial,&ids,g.links.len())}
fn graph_matches(s:&State,source:&Binding,inputs:&[(u32,u64,String)],node:u32,node_serial:u64,own:&[u32],link_count:usize)->bool {
 if !s.nodes.get(&node).is_some_and(|n|n.kind=="owned_stream"&&n.serial==node_serial)||!s.binding_current(source)||inputs.iter().any(|(id,serial,channel)|!s.ports.get(id).is_some_and(|p|p.input&&p.parent==node&&p.serial==*serial&&p.channel==*channel))||own.len()!=link_count{return false;}
 let actual:Vec<_>=s.links.iter().filter(|(_,l)|l.input_node==node).collect();
 actual.len()==own.len()&&actual.iter().all(|(id,l)|own.contains(id)&&source.ports.iter().any(|p|p.0==l.output_port&&l.output_node==source.node_id&&inputs.iter().any(|i|i.0==l.input_port&&i.2==p.2)))
}
#[cfg(test)] mod tests {
 use super::*;
 #[test] fn synthetic_component_link_lifetime(){
  let mut s=State::default();s.add_node(1,Node{serial:11,kind:"microphone",label:"mic".into()});
  s.add_port(2,Port{serial:12,parent:1,channel:"MONO".into(),monitor:false,input:false});
  s.add_node(3,Node{serial:13,kind:"owned_stream",label:"stream".into()});
  s.add_port(4,Port{serial:14,parent:3,channel:"MONO".into(),monitor:false,input:true});
  let b=Binding{node_id:1,node_serial:11,ports:vec![(2,12,"MONO".into())]};let inputs=vec![(4,14,"MONO".into())];
  s.links.insert(5,LinkRecord{serial:15,output_node:1,output_port:2,input_node:3,input_port:4});
  assert!(graph_matches(&s,&b,&inputs,3,13,&[5],1));
  let gate=Rc::new(Cell::new(true));s.guards.insert(3,Guard{source:b.clone(),inputs:inputs.clone(),node:3,node_serial:13,links:vec![(5,15)],gate:gate.clone()});
  s.add_node(99,Node{serial:99,kind:"microphone",label:"other".into()});assert!(graph_matches(&s,&b,&inputs,3,13,&[5],1));assert!(gate.get());
  s.links.insert(6,LinkRecord{serial:16,output_node:99,output_port:100,input_node:3,input_port:4});assert!(!graph_matches(&s,&b,&inputs,3,13,&[5],1));s.enforce();assert!(!gate.get());
  s.links.remove(&6);gate.set(true);s.remove(3);assert!(!gate.get());assert!(!graph_matches(&s,&b,&inputs,3,13,&[5],1));
  s.add_node(3,Node{serial:13,kind:"owned_stream",label:"stream".into()});s.remove(2);assert!(!graph_matches(&s,&b,&inputs,3,13,&[5],1));
 }
}
