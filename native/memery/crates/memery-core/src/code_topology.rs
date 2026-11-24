//! Private bounded indexed-topology capture contract. No source bytes or authority.
use serde::{Deserialize,Serialize};
use sha2::{Digest,Sha256};
use crate::code_query::{QueryError,QueryReceipt,MAX_QUERY_WORK};

pub const PROFILE:&str="indexed_topology_v1";
pub const MAX_BYTES:usize=64*1024*1024;
pub const MAX_PAGE_BYTES:usize=128*1024;
pub const MAX_ENVELOPE_BYTES:usize=262144;
#[derive(Debug,Clone,Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {pub files:usize,pub nodes:usize,pub edges:usize,pub work:usize,pub bytes:usize,pub page_bytes:usize}
#[derive(Debug,Clone,Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {pub version:u8,pub profile:String,pub paths:Vec<String>,pub expected_receipt:QueryReceipt,pub limits:Limits}
pub fn unavailable()->QueryError {QueryError{code:"unavailable".into(),message:"Indexed topology unavailable".into()}}
pub fn sha(bytes:&[u8])->String {format!("{:x}",Sha256::digest(bytes))}
pub fn canonical<T:Serialize>(v:&T)->Result<Vec<u8>,QueryError>{let value=serde_json::to_value(v).map_err(|_|unavailable())?;serde_json::to_vec(&value).map_err(|_|unavailable())}
/// Serialize canonical output without growing the retained byte buffer past its admitted limit.
pub fn canonical_bounded<T:Serialize>(v:&T,limit:usize)->Result<Vec<u8>,QueryError>{
 struct Bounded {bytes:Vec<u8>,limit:usize}
 impl std::io::Write for Bounded {fn write(&mut self,data:&[u8])->std::io::Result<usize>{if self.bytes.len().checked_add(data.len()).is_none_or(|n|n>self.limit){return Err(std::io::Error::other("topology output limit"));}self.bytes.extend_from_slice(data);Ok(data.len())}fn flush(&mut self)->std::io::Result<()> {Ok(())}}
 let value=serde_json::to_value(v).map_err(|_|unavailable())?;let mut out=Bounded{bytes:Vec::new(),limit};serde_json::to_writer(&mut out,&value).map_err(|_|unavailable())?;Ok(out.bytes)
}
impl Request {
 pub fn validate(&self)->Result<(),QueryError>{
  let l=&self.limits;let max_files=crate::code_index_limits::current().max_files;
  if self.version!=1||self.profile!=PROFILE||self.paths.is_empty()||self.paths.len()>l.files||l.files==0||l.files>max_files||l.nodes==0||l.nodes>MAX_QUERY_WORK||l.edges==0||l.edges>MAX_QUERY_WORK||l.work==0||l.work>MAX_QUERY_WORK||l.bytes<512||l.bytes>MAX_BYTES||l.page_bytes==0||l.page_bytes>MAX_PAGE_BYTES{return Err(unavailable());}
  let mut previous:Option<&str>=None;
  for p in &self.paths {if p.is_empty()||p.len()>4096||p.starts_with('/')||(p.as_bytes().len()>=2 && p.as_bytes()[0].is_ascii_alphabetic() && p.as_bytes()[1]==b':')||p.contains('\\')||p.split('/').any(|s|s.is_empty()||s=="."||s=="..")||p.chars().any(char::is_control)||previous.is_some_and(|x|x>=p.as_str()){return Err(unavailable());}previous=Some(p);}
  Ok(())
 }
}
#[derive(Debug,Clone,Serialize)]
pub struct Scope {pub workspace_id:String,pub registration_id:String,pub checkout_id:String,pub paths:Vec<String>}
#[derive(Debug,Clone,Serialize)]
pub struct Coverage {pub state:&'static str,pub reasons:Vec<&'static str>}
#[derive(Debug,Clone,Serialize)]
pub struct File {pub file_id:String,pub rel_path:String,pub source_hash:Option<String>,pub coverage:Coverage}
#[derive(Debug,Clone,Serialize)]
pub struct Node {pub id:String,pub file_id:String,pub kind:&'static str,pub identity_state:&'static str,pub source_hash:Option<String>,pub relation_kind:Option<&'static str>,pub resolution_state:Option<String>}
#[derive(Debug,Clone,Serialize)]
pub struct Relation {pub from:String,pub to:String,pub kind:String,pub state:String,pub count:usize}
#[derive(Debug,Clone,Serialize)]
pub struct Unknown {pub file_id:String,pub occurrence_id:String,pub reason:&'static str}
#[derive(Debug,Clone,Serialize)]
pub struct Artifact {pub version:u8,pub profile:&'static str,pub identity_version:u8,pub scope:Scope,pub receipt:QueryReceipt,pub producer_fingerprint:String,pub files:Vec<File>,pub nodes:Vec<Node>,pub relations:Vec<Relation>,pub unknown:Vec<Unknown>}
#[derive(Debug,Clone)]
pub struct Capture {pub bytes:Vec<u8>,pub result_sha256:String,pub receipt:QueryReceipt,pub scope_sha256:String,pub producer_fingerprint:String,pub file_count:usize,pub node_count:usize,pub relation_count:usize,pub page_bytes:usize}
