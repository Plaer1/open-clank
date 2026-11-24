//! Bounded, owned generic GCF section stream. Chunks are provisional until close.
use std::collections::HashSet;
use crate::{codec, parser::{self, Value, MAX_BYTES}};

pub const POLICY: &str = "gcf-generic-v3-stream-safe-integers-v1";
const MAX_VALUES: usize = 16_384;
const MAX_ROOT: usize = 256;
const MAX_FIELDS: usize = 256;
const HEADER: &str = "GCF profile=generic\n";

struct Table { name: String, fields: Vec<String>, rows: Vec<Value> }
pub struct GenericStream { frame: Vec<u8>, emitted: usize, entries: Vec<(String, Value)>, names: HashSet<String>, table: Option<Table>, counts: Vec<usize>, values: usize, terminal: bool }

fn scalar(v:&Value)->bool { matches!(v,Value::Null|Value::Bool(_)|Value::Integer(_)|Value::String(_)) }
fn text(v:&Value)->Result<&str,()> { if let Value::String(s)=v { Ok(s) } else { Err(()) } }
fn exact<'a>(v:&'a Value,keys:&[&str])->Result<Vec<&'a Value>,()> {
    let Value::Object(items)=v else{return Err(())};
    if items.len()!=keys.len(){return Err(())}
    keys.iter().map(|k|items.iter().find(|(name,_)|name==k).map(|(_,v)|v).ok_or(())).collect()
}
impl GenericStream {
    pub fn new()->Self { Self{frame:HEADER.as_bytes().to_vec(),emitted:0,entries:Vec::new(),names:HashSet::new(),table:None,counts:Vec::new(),values:1,terminal:false} }
    fn append(&mut self,s:&str)->Result<(),()> { if self.frame.len().checked_add(s.len()).filter(|n|*n<=MAX_BYTES).is_none(){return Err(())} self.frame.extend_from_slice(s.as_bytes());Ok(()) }
    fn reserve_name(&mut self,name:&str)->Result<(),()> { if name.is_empty() || self.names.len()>=MAX_ROOT || !self.names.insert(name.to_owned()){return Err(())} Ok(()) }
    fn add_values(&mut self,n:usize)->Result<(),()> { self.values=self.values.checked_add(n).filter(|n|*n<=MAX_VALUES).ok_or(())?;Ok(()) }
    fn check_tree(&self)->Result<(),()> {
        let mut entries=self.entries.clone();
        if let Some(t)=&self.table {entries.push((t.name.clone(),Value::Array(t.rows.clone())))}
        let value=Value::Object(entries);let bytes=codec::canonical_json(&value).map_err(|_|())?;
        if bytes.len()>MAX_BYTES || parser::parse(&bytes).map_err(|_|())?!=value {return Err(())} Ok(())
    }
    fn push_inner(&mut self,input:&[u8])->Result<Vec<u8>,()> {
        if self.terminal {return Err(())} let event=parser::parse(input).map_err(|_|())?;
        let Value::Object(items)=&event else{return Err(())};
        let op=items.iter().find(|(k,_)|k=="op").map(|(_,v)|text(v)).ok_or(())??;
        match op {
            "kv"=>{let v=exact(&event,&["op","name","value"])?;let name=text(v[1])?;if self.table.is_some()||!scalar(v[2]){return Err(())}self.reserve_name(name)?;self.add_values(1)?;let line=format!("{}={}\n",codec::stream_key(name).map_err(|_|())?,codec::stream_scalar(v[2]).map_err(|_|())?);self.append(&line)?;self.entries.push((name.to_owned(),v[2].clone()));},
            "begin"=>{let v=exact(&event,&["op","name","fields"])?;let name=text(v[1])?;let Value::Array(fields)=v[2] else{return Err(())};if self.table.is_some()||fields.is_empty()||fields.len()>MAX_FIELDS{return Err(())}let mut seen=HashSet::new();let mut names=Vec::new();for field in fields {let f=text(field)?;if f.is_empty()||f.contains('>')||!seen.insert(f.to_owned()){return Err(())}names.push(f.to_owned())}self.reserve_name(name)?;self.add_values(1)?;let keys=names.iter().map(|f|codec::stream_key(f).map_err(|_|())).collect::<Result<Vec<_>,_>>()?;self.append(&format!("## {} [?]{{{}}}\n",codec::stream_key(name).map_err(|_|())?,keys.join(",")))?;self.table=Some(Table{name:name.to_owned(),fields:names,rows:Vec::new()});},
            "row"=>{let v=exact(&event,&["op","values"])?;let Value::Array(values)=v[1] else{return Err(())};let fields=self.table.as_ref().ok_or(())?.fields.clone();if values.len()!=fields.len()||values.iter().any(|v|!scalar(v)){return Err(())}self.add_values(1+values.len())?;let cells=values.iter().map(|v|codec::stream_scalar(v).map_err(|_|())).collect::<Result<Vec<_>,_>>()?;self.append(&format!("{}\n",cells.join("|")))?;let row=Value::Object(fields.into_iter().zip(values.iter().cloned()).collect());self.table.as_mut().ok_or(())?.rows.push(row);},
            "end"=>{exact(&event,&["op"])?;let table=self.table.take().ok_or(())?;self.counts.push(table.rows.len());self.entries.push((table.name,Value::Array(table.rows)));},
            _=>return Err(())
        }
        self.check_tree()?;let out=self.frame[self.emitted..].to_vec();self.emitted=self.frame.len();Ok(out)
    }
    pub fn push(&mut self,input:&[u8])->Result<Vec<u8>,()> {let r=self.push_inner(input);if r.is_err(){self.terminal=true}r}
    pub fn close(&mut self)->Result<Vec<u8>,()> {
        if self.terminal || self.table.is_some(){self.terminal=true;return Err(())}
        self.terminal=true;
        if !self.counts.is_empty(){let counts=self.counts.iter().map(usize::to_string).collect::<Vec<_>>().join(",");self.append(&format!("##! summary counts={}\n",counts))?}
        let decoded=crate::stream_decoder::decode_stream(&self.frame).map_err(|_|())?;
        if parser::parse(&decoded).map_err(|_|())?!=Value::Object(self.entries.clone()){return Err(())}
        Ok(self.frame[self.emitted..].to_vec())
    }
    pub fn abort(&mut self) { self.terminal=true; self.frame.clear();self.entries.clear();self.table=None; }
}
