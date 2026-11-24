//! Private, same-core discovery. No audio stream or public protocol is enabled.
use std::{cell::RefCell, collections::BTreeMap, rc::Rc, time::{Duration,Instant}};
use pipewire as pw;

const MAX_OBJECTS:usize=512;
const MAX_CHOICES:usize=24;
const MAX_PORTS:usize=2;
#[derive(Clone, Debug, PartialEq, Eq)]
struct Node {serial:u64, kind:&'static str,label:String}
#[derive(Clone, Debug, PartialEq, Eq)]
struct Port {serial:u64,parent:u32,channel:String,monitor:bool,input:bool}
#[derive(Clone, Debug, PartialEq, Eq)]
struct LinkRecord {serial:u64,output_node:u32,output_port:u32,input_node:u32,input_port:u32}
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Binding {node_id:u32,node_serial:u64,ports:Vec<(u32,u64,String)>}
#[derive(Clone, Debug)]
#[derive(serde::Serialize)]
pub(crate) struct Choice {pub(crate) kind:&'static str,pub(crate) choice_id:String,pub(crate) label:String,pub(crate) channels:Vec<String>}
#[derive(Default)]
struct State {nodes:BTreeMap<u32,Node>,ports:BTreeMap<u32,Port>,links:BTreeMap<u32,LinkRecord>,guards:BTreeMap<u32,audio_links::Guard>,choices:BTreeMap<String,Binding>,epoch:u64,lost:bool,done:Option<i32>}
impl State {
 fn changed(&mut self){self.epoch=self.epoch.saturating_add(1);}
 fn add_node(&mut self,id:u32,node:Node){self.changed();self.nodes.insert(id,node);self.cap();self.enforce();}
 fn add_port(&mut self,id:u32,port:Port){self.changed();self.ports.insert(id,port);self.cap();self.enforce();}
 fn remove(&mut self,id:u32){self.changed();self.nodes.remove(&id);self.ports.remove(&id);self.links.remove(&id);self.enforce();}
 fn cap(&mut self){if self.nodes.len()+self.ports.len()+self.links.len()>MAX_OBJECTS{self.lose();}}
 fn enforce(&self){for guard in self.guards.values(){if !audio_links::guard_matches(self,guard){guard.gate.set(false);}}}
 fn lose(&mut self){for guard in self.guards.values(){guard.gate.set(false);}self.lost=true;self.nodes.clear();self.ports.clear();self.links.clear();self.choices.clear();}
 fn choices(&mut self)->Result<Vec<Choice>,()> {
  if self.lost{return Err(());}self.choices.clear();let mut result=Vec::new();
  for (&id,node) in &self.nodes {
   if node.kind=="owned_stream"{continue;}
   let mut ports:Vec<_>=self.ports.iter().filter(|(_,p)|p.parent==id && !p.input && p.monitor==(node.kind=="system_output"))
    .map(|(&pid,p)|(pid,p.serial,p.channel.clone())).collect();
   ports.sort_by(|a,b|a.2.cmp(&b.2));
   if ports.is_empty() || ports.len()>MAX_PORTS {continue;}
   if result.len()>=MAX_CHOICES {self.choices.clear();return Err(());}
   if !((ports.len()==1 && ports[0].2=="MONO") || (ports.len()==2 && ports.iter().any(|p|p.2=="FL") && ports.iter().any(|p|p.2=="FR"))) {continue;}
   let mut random=[0u8;16];let got=unsafe{libc::getrandom(random.as_mut_ptr().cast(),random.len(),libc::GRND_NONBLOCK)};
   if got!=16 {self.choices.clear();return Err(());}let token=random.iter().map(|x|format!("{x:02x}")).collect::<String>();
   if self.choices.contains_key(&token){self.choices.clear();return Err(());}
   let channels=ports.iter().map(|p|p.2.clone()).collect();
   self.choices.insert(token.clone(),Binding{node_id:id,node_serial:node.serial,ports});
   result.push(Choice{kind:node.kind,choice_id:token,label:node.label.clone(),channels});
  }Ok(result)
 }
 fn binding_current(&self,b:&Binding)->bool {if self.lost{return false;}let Some(n)=self.nodes.get(&b.node_id) else{return false;};if n.serial!=b.node_serial{return false;}b.ports.iter().all(|(id,serial,channel)|self.ports.get(id).is_some_and(|p|p.serial==*serial&&p.parent==b.node_id&&!p.input&&p.channel==*channel&&p.monitor==(n.kind=="system_output")))}
 fn resolve(&self,token:&str)->Option<&Binding>{let b=self.choices.get(token)?;if self.binding_current(b){Some(b)}else{None}}
}
fn label(value:&str)->String {let mut clean=String::new();for c in value.chars().filter(|c|!c.is_control()){if clean.len()+c.len_utf8()>64{break;}clean.push(c);}if clean.is_empty(){"Audio device".into()}else{clean}}
fn prop_num(props:&pw::spa::utils::dict::DictRef,key:&str)->Option<u64>{props.get(key)?.parse().ok()}

/// Must live on its creating thread. Holding this owns the only connection and listeners.
pub(crate) struct RegistryOwner {
 _registry_listener:pw::registry::Listener,_core_listener:pw::core::Listener,
 generation:String,registry:pw::registry::RegistryRc,core:pw::core::CoreRc,main:pw::main_loop::MainLoopRc,state:Rc<RefCell<State>>,
}
impl RegistryOwner {
 pub(crate) fn open(generation:&str)->Result<Self,()> {
  if generation.len()!=32 || !generation.bytes().all(|c|c.is_ascii_digit()||(b'a'..=b'f').contains(&c)){return Err(());}
  pw::init();let main=pw::main_loop::MainLoopRc::new(None).map_err(|_|())?;
  let context=pw::context::ContextRc::new(&main,None).map_err(|_|())?;
  let core=context.connect_rc(None).map_err(|_|())?;let registry=core.get_registry_rc().map_err(|_|())?;
  let state=Rc::new(RefCell::new(State::default()));let a=state.clone();let b=state.clone();
  let listener=registry.add_listener_local().global(move|g|{
   let is_link=g.type_.to_str()=="PipeWire:Interface:Link";let Some(p)=g.props else{if is_link{a.borrow_mut().lose();}return;};let Some(serial)=prop_num(p,"object.serial") else{if is_link{a.borrow_mut().lose();}return;};
   match g.type_.to_str(){
    "PipeWire:Interface:Node"=>{let kind=match p.get("media.class"){Some("Audio/Source")=>"microphone",Some("Audio/Sink")=>"system_output",Some("Stream/Input/Audio")=>"owned_stream",_=>return};let name=label(p.get("node.description").or_else(||p.get("node.name")).unwrap_or("Audio device"));a.borrow_mut().add_node(g.id,Node{serial,kind,label:name});},
    "PipeWire:Interface:Port"=>{let input=match p.get("port.direction"){Some("in")=>true,Some("out")=>false,_=>return};let Some(parent)=prop_num(p,"node.id").and_then(|v|u32::try_from(v).ok()) else{return;};let channel=match p.get("audio.channel"){Some("FL")=>"FL",Some("FR")=>"FR",Some("MONO")=>"MONO",_=>return}.to_owned();a.borrow_mut().add_port(g.id,Port{serial,parent,channel,monitor:p.get("port.monitor")==Some("true"),input});},
    "PipeWire:Interface:Link"=>{let get=|k|prop_num(p,k).and_then(|v|u32::try_from(v).ok());let (Some(output_node),Some(output_port),Some(input_node),Some(input_port))=(get("link.output.node"),get("link.output.port"),get("link.input.node"),get("link.input.port")) else{a.borrow_mut().lose();return;};let mut state=a.borrow_mut();state.changed();state.links.insert(g.id,LinkRecord{serial,output_node,output_port,input_node,input_port});state.cap();state.enforce();},_=>{}
   }
  }).global_remove(move|id|b.borrow_mut().remove(id)).register();
  let c=state.clone();let d=state.clone();let core_listener=core.add_listener_local().done(move|_,seq|c.borrow_mut().done=Some(seq.seq())).error(move|_,_,_,_|d.borrow_mut().lose()).register();
  Ok(Self{_registry_listener:listener,_core_listener:core_listener,generation:generation.to_owned(),registry,core,main,state})
 }
 pub(crate) fn snapshot(&self,timeout:Duration)->Result<Vec<Choice>,()> {
  if timeout>Duration::from_secs(2){return Err(());}let seq=self.core.sync(0).map_err(|_|())?.seq();let end=Instant::now()+timeout;
  while self.state.borrow().done!=Some(seq) {if self.state.borrow().lost||Instant::now()>=end{return Err(());}let wait=end.saturating_duration_since(Instant::now()).min(Duration::from_millis(20));if self.main.loop_().iterate(pw::loop_::Timeout::Finite(wait))<0{return Err(());}}
  self.state.borrow_mut().choices()
 }
 pub(crate) fn poll(&self)->Result<(),()> {if self.main.loop_().iterate(pw::loop_::Timeout::None)<0{self.state.borrow_mut().lose();}if self.state.borrow().lost{Err(())}else{Ok(())}}
 pub(crate) fn validate(&self,generation:&str,choice:&str)->bool {self.generation==generation && self.state.borrow().resolve(choice).is_some()}
 pub(crate) fn freeze(&self,generation:&str,mic:Option<&str>,system:Option<&str>)->Result<Vec<(&'static str,Binding)>,()> {
  if self.generation!=generation{return Err(());}freeze_state(&self.state.borrow(),mic,system)
 }
 // Stream/link preparation lives in the child module and never opens a second core.
}

fn freeze_state(state:&State,mic:Option<&str>,system:Option<&str>)->Result<Vec<(&'static str,Binding)>,()> {
 let mut out=Vec::new();for (kind,token) in [("microphone",mic),("system_output",system)] {if let Some(token)=token {let b=state.resolve(token).cloned().ok_or(())?;if state.nodes.get(&b.node_id).map(|n|n.kind)!=Some(kind)||out.iter().any(|(_,other):&(&str,Binding)|other.node_id==b.node_id){return Err(());}out.push((kind,b));}}Ok(out)
}
#[cfg(test)] pub(crate) fn synthetic_dual_freeze()->bool {
 let mut s=State::default();s.add_node(1,Node{serial:11,kind:"microphone",label:"mic".into()});s.add_node(2,Node{serial:12,kind:"system_output",label:"sink".into()});
 s.add_port(3,Port{serial:13,parent:1,channel:"MONO".into(),monitor:false,input:false});s.add_port(4,Port{serial:14,parent:2,channel:"MONO".into(),monitor:true,input:false});
 let Ok(choices)=s.choices() else{return false;};let Some(m)=choices.iter().find(|x|x.kind=="microphone") else{return false;};let Some(o)=choices.iter().find(|x|x.kind=="system_output") else{return false;};
 s.add_node(8,Node{serial:18,kind:"owned_stream",label:"unrelated".into()});
 let Ok(frozen)=freeze_state(&s,Some(&m.choice_id),Some(&o.choice_id)) else{return false;};s.add_node(9,Node{serial:19,kind:"owned_stream",label:"owned".into()});
 if !frozen.iter().all(|(_,b)|s.binding_current(b)){return false;}s.remove(4);!s.binding_current(&frozen[1].1)&&s.binding_current(&frozen[0].1)
}
#[path = "audio_links.rs"]
mod audio_links;
pub(crate) use audio_links::Prepared;

#[cfg(test)] mod tests {use super::*;
 #[test] fn synthetic_registry_lifetime(){let mut s=State::default();s.add_node(9,Node{serial:41,kind:"microphone",label:label("Mic\nName")});s.add_port(12,Port{serial:44,parent:9,channel:"MONO".into(),monitor:false,input:false});let choices=s.choices().unwrap();assert_eq!(choices.len(),1);assert_eq!(choices[0].label,"MicName");assert!(s.resolve(&choices[0].choice_id).is_some());s.remove(12);assert!(s.resolve(&choices[0].choice_id).is_none());s.lose();assert!(s.choices().is_err());}
}
