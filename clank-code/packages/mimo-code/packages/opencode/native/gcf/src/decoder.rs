//! Bounded reader for this crate's canonical generic-v3 encoder output.
use crate::{codec, parser::{self, Value, MAX_BYTES}};
use std::collections::HashSet;
const MAX_VALUES:usize=16_384;
const MAX_DEPTH:usize=32;
#[derive(Debug,PartialEq,Eq)]
pub enum DecodeError { Invalid, TooLarge, TooDeep, TooManyValues }

pub(crate) fn split_unquoted(s:&str, sep:u8)->Result<Vec<&str>,DecodeError>{
 let mut result=Vec::new();let mut start=0;let mut quoted=false;let mut escape=false;
 for (i,b) in s.bytes().enumerate(){if quoted {if escape {escape=false} else if b==b'\\' {escape=true} else if b==b'"' {quoted=false}} else if b==b'"'{quoted=true} else if b==sep {result.push(&s[start..i]);start=i+1}}
 if quoted||escape{return Err(DecodeError::Invalid)} result.push(&s[start..]);Ok(result)
}
pub(crate) fn key(s:&str)->Result<String,DecodeError>{
 if s.starts_with('"'){if let Value::String(v)=parser::parse(s.as_bytes()).map_err(|_|DecodeError::Invalid)?{return Ok(v)}return Err(DecodeError::Invalid)}
 let mut c=s.bytes();if !matches!(c.next(),Some(b'a'..=b'z'|b'A'..=b'Z'|b'_'))||!c.all(|b|b.is_ascii_alphanumeric()||b==b'_'){return Err(DecodeError::Invalid)} Ok(s.to_owned())
}
pub(crate) fn atom(s:&str)->Result<Value,DecodeError>{
 if s=="-"{return Ok(Value::Null)}
 let v=parser::parse(s.as_bytes()).map_err(|_|DecodeError::Invalid)?;
 if matches!(v,Value::Array(_)|Value::Object(_)|Value::Null){return Err(DecodeError::Invalid)} Ok(v)
}
fn json_string_bytes(s:&str)->usize {
  2 + s.chars().map(|c|match c {'"'|'\\'|'\u{8}'|'\u{c}'|'\n'|'\r'|'\t'=>2, c if (c as u32)<32=>6, c=>c.len_utf8()}).sum::<usize>()
}
struct Reader<'a>{lines:Vec<&'a str>,at:usize,values:usize,bytes:usize}
impl<'a> Reader<'a>{
 fn count(&mut self,depth:usize)->Result<(),DecodeError>{if depth>MAX_DEPTH{return Err(DecodeError::TooDeep)}self.values+=1;if self.values>MAX_VALUES{return Err(DecodeError::TooManyValues)}Ok(())}
 fn charge(&mut self,n:usize)->Result<(),DecodeError>{self.bytes=self.bytes.checked_add(n).filter(|v|*v<=MAX_BYTES).ok_or(DecodeError::TooLarge)?;Ok(())}
 fn member(&mut self,k:&str,first:bool)->Result<(),DecodeError>{self.charge(json_string_bytes(k)+1+usize::from(!first))}
 fn scalar(&mut self,s:&str,depth:usize)->Result<Value,DecodeError>{self.count(depth)?;self.charge(if s=="-"{4}else{s.len()})?;atom(s)}
 fn line(&self,depth:usize)->Option<&'a str>{let l=*self.lines.get(self.at)?;let prefix="  ".repeat(depth);let tail=l.strip_prefix(&prefix)?;if tail.starts_with(' '){None}else{Some(tail)}}
 fn object(&mut self,depth:usize)->Result<Value,DecodeError>{
  self.count(depth+1)?;self.charge(2)?;let mut pairs=Vec::new();let mut seen=HashSet::new();
  while let Some(line)=self.line(depth){if line.starts_with('@'){break}self.at+=1;
   let (k,v)=if let Some(section)=line.strip_prefix("## "){
    if let Some(pos)=unquoted_bracket(section){let k=key(section[..pos].trim_end())?;self.member(&k,pairs.is_empty())?;let value=self.array(&section[pos..],depth,false)?;(k,value)}
    else {let k=key(section)?;self.member(&k,pairs.is_empty())?;let value=self.object(depth+1)?;(k,value)}
   }else if let Some(pos)=unquoted_bracket(line){let k=key(&line[..pos])?;self.member(&k,pairs.is_empty())?;let value=self.array(&line[pos..],depth,false)?;(k,value)}
   else {let parts=split_unquoted(line,b'=')?;if parts.len()!=2{return Err(DecodeError::Invalid)}let k=key(parts[0])?;self.member(&k,pairs.is_empty())?;(k,self.scalar(parts[1],depth+2)?)};
   if !seen.insert(k.clone()){return Err(DecodeError::Invalid)}pairs.push((k,v));
  }Ok(Value::Object(pairs))
 }
 fn array(&mut self,header:&str,depth:usize,root:bool)->Result<Value,DecodeError>{
  self.count(depth+1)?;self.charge(2)?;let end=header.find(']').ok_or(DecodeError::Invalid)?;let count=header.get(1..end).ok_or(DecodeError::Invalid)?.parse::<usize>().map_err(|_|DecodeError::Invalid)?;if count>MAX_VALUES{return Err(DecodeError::TooManyValues)}let rest=&header[end+1..];let mut out=Vec::new();
  if let Some(body)=rest.strip_prefix(": "){let cells=split_unquoted(body,b',')?;if cells.len()!=count{return Err(DecodeError::Invalid)}for c in cells{if !out.is_empty(){self.charge(1)?}out.push(self.scalar(c,depth+2)?)} }
  else if rest.starts_with('{')&&rest.ends_with('}'){
   let fields:Vec<String>=split_unquoted(&rest[1..rest.len()-1],b',')?.into_iter().map(key).collect::<Result<_,_>>()?;let mut seen=HashSet::new();if fields.is_empty()||fields.iter().any(|k|!seen.insert(k.clone())){return Err(DecodeError::Invalid)}
   for _ in 0..count{if !out.is_empty(){self.charge(1)?}let row=self.line(depth).ok_or(DecodeError::Invalid)?;self.at+=1;let cells=split_unquoted(row,b'|')?;if cells.len()!=fields.len(){return Err(DecodeError::Invalid)}self.count(depth+2)?;self.charge(2)?;let mut obj=Vec::new();for (name,cell) in fields.iter().zip(cells){if cell!="~"{self.member(name,obj.is_empty())?;let value=self.scalar(cell,depth+3)?;obj.push((name.clone(),value))}}out.push(Value::Object(obj));}
  }else if rest.is_empty(){for i in 0..count{if i>0{self.charge(1)?}let line=self.line(depth).ok_or(DecodeError::Invalid)?;self.at+=1;let prefix=format!("@{} ",i);let body=line.strip_prefix(&prefix).ok_or(DecodeError::Invalid)?;let value=if body=="{}"{self.object(depth+1)?}else if body.starts_with('['){self.array(body,depth+1,false)?}else if let Some(v)=body.strip_prefix('='){self.scalar(v,depth+2)?}else{return Err(DecodeError::Invalid)};out.push(value)}}else{return Err(DecodeError::Invalid)}
  let _=root;Ok(Value::Array(out))
 }
}
pub(crate) fn unquoted_bracket(s:&str)->Option<usize>{let mut quoted=false;let mut escape=false;for(i,b)in s.bytes().enumerate(){if quoted{if escape{escape=false}else if b==b'\\'{escape=true}else if b==b'"'{quoted=false}}else if b==b'"'{quoted=true}else if b==b'['{return Some(i)}}None}
/// Decode only canonical bytes actually emitted by the current generic-v3 encoder.
pub fn decode(input:&[u8])->Result<Vec<u8>,DecodeError>{
 if input.len()>MAX_BYTES{return Err(DecodeError::TooLarge)}let text=std::str::from_utf8(input).map_err(|_|DecodeError::Invalid)?;
 let rest=text.strip_prefix("GCF profile=generic\n").ok_or(DecodeError::Invalid)?;if !rest.is_empty()&&!rest.ends_with('\n'){return Err(DecodeError::Invalid)}
 let lines=if rest.is_empty(){Vec::new()}else{rest[..rest.len()-1].split('\n').collect()};let mut r=Reader{lines,at:0,values:0,bytes:0};
 let v=if let Some(first)=r.lines.first().copied(){if let Some(s)=first.strip_prefix('='){r.at=1;r.scalar(s,1)?}else if let Some(h)=first.strip_prefix("## ["){r.at=1;r.array(&format!("[{}",h),0,true)?}else{r.object(0)?}}else{r.object(0)?};
 if r.at!=r.lines.len()||codec::encode_value(&v).map_err(|_|DecodeError::Invalid)?!=input{return Err(DecodeError::Invalid)}
 let json=codec::canonical_json(&v).map_err(|_|DecodeError::TooLarge)?;
 match parser::parse(&json) {
  Ok(parsed) if parsed==v=>Ok(json),
  Err(parser::Reject::TooDeep)=>Err(DecodeError::TooDeep),
  Err(parser::Reject::TooManyValues)=>Err(DecodeError::TooManyValues),
  Err(parser::Reject::TooLarge)=>Err(DecodeError::TooLarge),
  _=>Err(DecodeError::Invalid),
 }
}
