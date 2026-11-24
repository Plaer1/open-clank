//! Stateless GCF generic v3, with nested values kept unflattened.
//! Grammar studied from gcf-go v1.7.1 (MIT); see the adjacent NOTICE.
use crate::parser::{self, Reject, Value, MAX_BYTES};

#[derive(Debug, PartialEq, Eq)]
pub enum Outcome { Encoded(Vec<u8>), Bypass(Reason) }
#[derive(Debug, PartialEq, Eq)]
pub enum Reason { TooSmall, TooLarge, Invalid, UnsupportedNumber, TooDeep, TooManyValues, DuplicateKey, NotSmaller }
struct Writer { text: String }
impl Writer {
    fn add(&mut self, s: &str) -> Result<(),Reason> {
        if self.text.len().checked_add(s.len()).filter(|n| *n <= MAX_BYTES).is_none() { return Err(Reason::TooLarge); }
        self.text.push_str(s); Ok(())
    }
    fn indent(&mut self, depth: usize) -> Result<(),Reason> { for _ in 0..depth { self.add("  ")?; } Ok(()) }
}
fn json_string(w: &mut Writer, s: &str) -> Result<(),Reason> {
    w.add("\"")?;
    for c in s.chars() { match c {
        '"'=>w.add("\\\"")?, '\\'=>w.add("\\\\")?, '\u{8}'=>w.add("\\b")?, '\u{c}'=>w.add("\\f")?,
        '\n'=>w.add("\\n")?, '\r'=>w.add("\\r")?, '\t'=>w.add("\\t")?,
        c if (c as u32)<32 => w.add(&format!("\\u{:04x}",c as u32))?,
        c => { let mut b=[0;4];w.add(c.encode_utf8(&mut b))?; }
    }}
    w.add("\"")
}
fn key(w: &mut Writer, s: &str) -> Result<(),Reason> {
    let mut chars=s.bytes();
    if matches!(chars.next(),Some(b'a'..=b'z'|b'A'..=b'Z'|b'_')) && chars.all(|b| b.is_ascii_alphanumeric() || b==b'_') { w.add(s) }
    else { json_string(w,s) }
}
fn scalar(w: &mut Writer,v: &Value) -> Result<(),Reason> { match v {
    Value::Null=>w.add("-"),Value::Bool(true)=>w.add("true"),Value::Bool(false)=>w.add("false"),
    Value::Integer(n)=>w.add(&n.to_string()),Value::String(s)=>json_string(w,s),_=>Err(Reason::Invalid),
} }
pub(crate) fn stream_key(value:&str)->Result<String,Reason>{let mut w=Writer{text:String::new()};key(&mut w,value)?;Ok(w.text)}
pub(crate) fn stream_scalar(value:&Value)->Result<String,Reason>{let mut w=Writer{text:String::new()};scalar(&mut w,value)?;Ok(w.text)}
fn primitive(v: &Value)->bool { !matches!(v,Value::Array(_)|Value::Object(_)) }
fn compact(w: &mut Writer,v: &Value)->Result<(),Reason> { match v {
    Value::Null=>w.add("null"),Value::Bool(true)=>w.add("true"),Value::Bool(false)=>w.add("false"),
    Value::Integer(n)=>w.add(&n.to_string()),Value::String(s)=>json_string(w,s),
    Value::Array(items)=>{w.add("[")?;for (i,v) in items.iter().enumerate(){if i>0{w.add(",")?}compact(w,v)?}w.add("]")},
    Value::Object(items)=>{w.add("{")?;for (i,(k,v)) in items.iter().enumerate(){if i>0{w.add(",")?}json_string(w,k)?;w.add(":")?;compact(w,v)?}w.add("}")},
} }
fn object(w: &mut Writer,items:&[(String,Value)],depth:usize)->Result<(),Reason>{
    for (k,v) in items { w.indent(depth)?;match v {
        Value::Object(values)=>{w.add("## ")?;key(w,k)?;w.add("\n")?;object(w,values,depth+1)?},
        Value::Array(values)=>{array(w,values,depth,Some(k),false)?},
        _=>{key(w,k)?;w.add("=")?;scalar(w,v)?;w.add("\n")?},
    }} Ok(())
}
fn scalar_object_fields(items:&[Value])->Option<Vec<&str>>{
    let mut fields=Vec::new();
    let mut indices=std::collections::HashMap::new();
    for item in items { let Value::Object(values)=item else{return None};
        for (k,v) in values { if !primitive(v) || k.contains('>') {return None} if !indices.contains_key(k.as_str()) {indices.insert(k.as_str(),fields.len());fields.push(k.as_str())} }
    }
    for item in items {
        let Value::Object(values)=item else{return None};
        let mut last=None;
        for (k,_) in values { let i=indices[k.as_str()]; if last.is_some_and(|n| i<=n) {return None} last=Some(i); }
    }
    if fields.is_empty(){None}else{Some(fields)}
}
fn array(w:&mut Writer,items:&[Value],depth:usize,name:Option<&str>,root:bool)->Result<(),Reason>{
    let all_scalar=items.iter().all(primitive);
    let fields=if !all_scalar {scalar_object_fields(items)} else {None};
    if root || (name.is_some() && (!all_scalar || items.is_empty())){w.add("## ")?;}
    if let Some(name)=name {key(w,name)?;if root || !all_scalar || items.is_empty(){w.add(" ")?;}}
    w.add(&format!("[{}]",items.len()))?;
    if let Some(fields)=fields {
        w.add("{")?;for (i,f) in fields.iter().enumerate(){if i>0{w.add(",")?}key(w,f)?}w.add("}\n")?;
        for item in items { let Value::Object(values)=item else{unreachable!()};w.indent(depth)?;
            let cells:std::collections::HashMap<&str,&Value>=values.iter().map(|(k,v)|(k.as_str(),v)).collect();
            for (i,f) in fields.iter().enumerate(){if i>0{w.add("|")?}match cells.get(f){Some(v)=>scalar(w,v)?,None=>w.add("~")?}}w.add("\n")?; }
    } else if all_scalar && !items.is_empty() {
        w.add(": ")?;for (i,v) in items.iter().enumerate(){if i>0{w.add(",")?}scalar(w,v)?}w.add("\n")?;
    } else {
        w.add("\n")?;
        for (i,v) in items.iter().enumerate(){w.indent(depth)?;w.add(&format!("@{} ",i))?;match v {
            Value::Object(values)=>{w.add("{}\n")?;object(w,values,depth+1)?},
            Value::Array(values)=>{array(w,values,depth+1,None,false)?},
            _=>{w.add("=")?;scalar(w,v)?;w.add("\n")?},
        }}
    }
    Ok(())
}
pub(crate) fn encode_value(v:&Value)->Result<Vec<u8>,Reason>{
    let mut w=Writer{text:String::new()};w.add("GCF profile=generic\n")?;
    match v {Value::Object(items)=>object(&mut w,items,0)?,Value::Array(items)=>array(&mut w,items,0,None,true)?,_=>{w.add("=")?;scalar(&mut w,v)?;w.add("\n")?}}
    Ok(w.text.into_bytes())
}
pub fn encode(input:&[u8])->Outcome{
    if input.len()<100{return Outcome::Bypass(Reason::TooSmall)}
    let v=match parser::parse(input){Ok(v)=>v,Err(e)=>return Outcome::Bypass(match e{
        Reject::TooLarge=>Reason::TooLarge,Reject::NumericDomain=>Reason::UnsupportedNumber,Reject::TooDeep=>Reason::TooDeep,
        Reject::TooManyValues=>Reason::TooManyValues,Reject::DuplicateKey=>Reason::DuplicateKey,_=>Reason::Invalid})};
    let mut compact_json=Writer{text:String::new()};if let Err(e)=compact(&mut compact_json,&v){return Outcome::Bypass(e)}
    match encode_value(&v){Ok(out) if out.len()<compact_json.text.len()=>Outcome::Encoded(out),Ok(_)=>Outcome::Bypass(Reason::NotSmaller),Err(e)=>Outcome::Bypass(e)}
}

/// Explicit bounded conversion; the automatic encoder keeps its size gates.
pub fn encode_preview(input:&[u8])->Outcome {
    let value=match parser::parse(input){Ok(value)=>value,Err(error)=>return Outcome::Bypass(match error {
        Reject::TooLarge=>Reason::TooLarge,Reject::NumericDomain=>Reason::UnsupportedNumber,Reject::TooDeep=>Reason::TooDeep,
        Reject::TooManyValues=>Reason::TooManyValues,Reject::DuplicateKey=>Reason::DuplicateKey,_=>Reason::Invalid})};
    // Both the canonical input and the actual wire are bounded by the existing writer.
    if let Err(error)=canonical_json(&value) {return Outcome::Bypass(error)}
    let output=match encode_value(&value){Ok(output)=>output,Err(error)=>return Outcome::Bypass(error)};
    let decoded=match crate::decoder::decode(&output){Ok(decoded)=>decoded,Err(_)=>return Outcome::Bypass(Reason::Invalid)};
    match parser::parse(&decoded) {
        Ok(roundtrip) if roundtrip==value=>Outcome::Encoded(output),
        _=>Outcome::Bypass(Reason::Invalid),
    }
}

pub(crate) fn canonical_json(v:&Value)->Result<Vec<u8>,Reason>{ let mut w=Writer{text:String::new()}; compact(&mut w,v)?; Ok(w.text.into_bytes()) }
