//! Private v7 crop selection validation. No capture authority or public capability.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Rect { pub x:u32, pub y:u32, pub width:u32, pub height:u32 }
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Crop { pub mode:String, pub basis_id:String, pub rect:Option<Rect> }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selection {
 pub version:u8, pub action:String, pub generation:String, pub selection_id:String,
 pub selection_digest:String, pub microphone:Option<String>, pub system_output:Option<String>, pub crop:Crop,
}
fn hex(s:&str,n:usize)->bool {s.len()==n && s.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
impl Selection {
 pub(crate) fn validate(&self,generation:&str,basis_id:&str,width:u32,height:u32,mic_offered:bool,system_offered:bool)->Result<Rect,()> {
  if self.version!=7 || self.action!="select" || self.generation!=generation || !hex(generation,32) || !hex(&self.selection_id,32)
    || !hex(&self.selection_digest,64) || self.crop.basis_id!=basis_id || !hex(basis_id,32)
    || width==0 || height==0 || width>16384 || height>16384
    || self.microphone.as_ref().is_some_and(|v|!mic_offered||!hex(v,32))
    || self.system_output.as_ref().is_some_and(|v|!system_offered||!hex(v,32)) {return Err(());}
  let (rect,coords)=match (self.crop.mode.as_str(),self.crop.rect.as_ref()) {
   ("full",None)=>(Rect{x:0,y:0,width,height},[None,None,None,None]),
   ("crop",Some(r)) if r.width>0 && r.height>0 && (r.x|r.y|r.width|r.height)&1==0
    && r.x.checked_add(r.width).is_some_and(|n|n<=width) && r.y.checked_add(r.height).is_some_and(|n|n<=height)
    =>(r.clone(),[Some(r.x),Some(r.y),Some(r.width),Some(r.height)]),
   _=>return Err(())
  };
  let payload=serde_json::json!(["openclank.recording.selection.v2",self.generation,self.selection_id,self.microphone,self.system_output,self.crop.mode,self.crop.basis_id,coords[0],coords[1],coords[2],coords[3]]);
  let bytes=serde_json::to_vec(&payload).map_err(|_|())?;
  if format!("{:x}",Sha256::digest(bytes))!=self.selection_digest{return Err(());}Ok(rect)
 }
}
