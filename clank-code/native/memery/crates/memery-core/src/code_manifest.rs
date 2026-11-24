//! Bounded package-manifest dependency keys and literal local import aliases.
use super::{error, limit, EvidenceError, EvidenceErrorCode, ExtractionBudget};
use serde::Deserialize;
use std::collections::HashSet;
use sha2::{Digest, Sha256};

pub(super) struct Key { pub section: String, pub name: String, pub start: usize, pub end: usize }

pub(super) struct ImportAlias {
    pub name: String,
    pub path: String,
    pub key_start: usize,
    pub key_end: usize,
    pub value_end: usize,
}

pub(super) fn import_alias_name(name: &str) -> bool {
    let Some(name) = name.strip_prefix('#') else { return false };
    !name.is_empty() && name.len() <= 128 && name.split('/').all(|part|
        !part.is_empty() && part != "." && part != ".."
            && part.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.')))
}

pub(super) fn local_import_path(path: &str) -> bool {
    let Some(path) = path.strip_prefix("./") else { return false };
    !path.is_empty() && path.len() <= 512 && path.split('/').all(|part|
        !part.is_empty() && part != "." && part != ".."
            && part != "node_modules"
            && part.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.')))
        && matches!(path.rsplit('.').next(), Some("js" | "jsx" | "mjs" | "cjs" | "ts" | "tsx" | "mts" | "cts"))
}

pub(super) fn package_root(value: &str) -> Option<&str> {
    if value.is_empty() || !value.is_ascii() || value.contains('\\') || value.starts_with('.') || value.starts_with('/') || value.starts_with("node:") { return None; }
    let mut pieces=value.split('/');
    let first=pieces.next()?;
    let root=if first.starts_with('@') {
        let second=pieces.next()?;
        let len=first.len()+1+second.len();
        &value[..len]
    } else { first };
    if root.len()>214 || root.is_empty() {return None;}
    if matches!(root, "assert"|"async_hooks"|"buffer"|"child_process"|"cluster"|"console"|"constants"|"crypto"|"dgram"|"diagnostics_channel"|"dns"|"domain"|"events"|"fs"|"http"|"http2"|"https"|"module"|"net"|"os"|"path"|"perf_hooks"|"process"|"punycode"|"querystring"|"readline"|"repl"|"stream"|"string_decoder"|"sys"|"timers"|"tls"|"trace_events"|"tty"|"url"|"util"|"v8"|"vm"|"wasi"|"worker_threads"|"zlib") { return None; }
    let valid=|part:&str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b,b'-'|b'_'|b'.'));
    if let Some(scoped)=root.strip_prefix('@') {let (scope,name)=scoped.split_once('/')?;if !valid(scope)||!valid(name){return None;}}
    else if !valid(root){return None;}
    if pieces.any(|part|part.is_empty()||part=="."||part==".."||!part.bytes().all(|b|b.is_ascii_alphanumeric()||matches!(b,b'-'|b'_'|b'.'))) {return None;}
    Some(root)
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum ManifestComparison {
 Match { key: (usize, usize), value: (usize, usize) },
 NoMatch,
 Unsupported,
}
struct Compare { expected: [u8; 32], found: Option<((usize,usize),(usize,usize))>, unsupported: bool }

// This is a lexical profile, not package-manager path resolution. Only leading
// parent components are canonical; no path is opened or returned.
fn canonical_file_spec(value: &str) -> bool {
 let Some(path)=value.strip_prefix("file:") else { return false; };
 if value.len()>512 || !value.is_ascii() || path.is_empty() || path.starts_with('/') { return false; }
 let mut normal=false; let mut count=0;
 for part in path.split('/') {
  count+=1; if count>32 || part.is_empty() || part=="." {return false;}
  if part==".." {if normal {return false;} continue;}
  if !part.bytes().all(|b|b.is_ascii_alphanumeric() || matches!(b,b'-'|b'_'|b'.'|b'@')) {return false;}
  normal=true;
 }
 normal
}
#[derive(Default)]
pub(super) struct LibraryFields { pub name:Option<String>, pub version:Option<String>, pub types:Option<String> }
struct Scanner<'a> { bytes:&'a [u8], index:usize, nodes:usize, budget:&'a ExtractionBudget, keys:Vec<Key>, unsupported:usize, compare:Option<Compare>, library:Option<LibraryFields>, aliases:Option<Vec<ImportAlias>> }
impl Scanner<'_> {
 fn tick(&mut self, depth:usize)->Result<(),EvidenceError>{ self.nodes=self.nodes.saturating_add(1);limit(self.nodes<=self.budget.max_ast_nodes && depth<=self.budget.max_depth.min(128),"manifest traversal limit") }
 fn ws(&mut self){while self.index<self.bytes.len() && matches!(self.bytes[self.index],b' '|b'\n'|b'\r'|b'\t'){self.index+=1;}}
 fn string(&mut self,decode:bool)->Result<(Option<String>,usize,usize),EvidenceError>{
  let start=self.index; if self.bytes.get(self.index)!=Some(&b'"'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest string expected"));}self.index+=1;
  while self.index<self.bytes.len(){let byte=self.bytes[self.index];self.index+=1;if byte==b'\\'{self.index+=1;}else if byte==b'"'{let end=self.index;let decoded=if decode{Some(serde_json::from_slice::<String>(&self.bytes[start..end]).map_err(|_|error(EvidenceErrorCode::InvalidSource,"manifest key invalid"))?)}else{None};return Ok((decoded,start,end));}}
  Err(error(EvidenceErrorCode::InvalidSource,"manifest string unfinished"))
 }
 fn value(&mut self,depth:usize,section:Option<&str>)->Result<(),EvidenceError>{
  self.tick(depth)?;self.ws();match self.bytes.get(self.index).copied(){
   Some(b'{')=>self.object(depth,section),Some(b'[')=>{self.index+=1;self.ws();if self.bytes.get(self.index)==Some(&b']'){self.index+=1;return Ok(());}loop{self.value(depth+1,None)?;self.ws();match self.bytes.get(self.index){Some(b',')=>self.index+=1,Some(b']')=>{self.index+=1;break},_=>return Err(error(EvidenceErrorCode::InvalidSource,"manifest array invalid"))}}Ok(())},
   Some(b'"')=>{self.string(false)?;Ok(())},Some(_)=>{while self.index<self.bytes.len()&&!matches!(self.bytes[self.index],b','|b']'|b'}'|b' '|b'\n'|b'\r'|b'\t'){self.index+=1;}Ok(())},None=>Err(error(EvidenceErrorCode::InvalidSource,"manifest value missing"))
  }
 }
 fn object(&mut self,depth:usize,section:Option<&str>)->Result<(),EvidenceError>{
  self.index+=1;let mut seen=HashSet::new();self.ws();if self.bytes.get(self.index)==Some(&b'}'){self.index+=1;return Ok(());}loop{
   self.tick(depth)?;self.ws();let (name,start,end)=self.string(true)?;let name=name.unwrap();if !seen.insert(name.clone()){return Err(error(EvidenceErrorCode::InvalidSource,"duplicate decoded manifest key"));}
   self.ws();if self.bytes.get(self.index)!=Some(&b':'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest separator invalid"));}self.index+=1;self.ws();
   if depth==0 && self.library.is_some() && matches!(name.as_str(),"name"|"version"|"types"|"typings") {
    if self.bytes.get(self.index)!=Some(&b'"'){return Err(error(EvidenceErrorCode::InvalidSource,"library metadata must be a string"));}
    let start=self.index;let (value,_,end)=self.string(true)?;self.index=start;
    let value=value.unwrap();
    if value.is_empty() || value.len()>512 || value.chars().any(|c|c.is_control()) {return Err(error(EvidenceErrorCode::InvalidSource,"library metadata unavailable"));}
    let fields=self.library.as_mut().unwrap();
    match name.as_str(){
     "name"=>fields.name=Some(value),"version"=>fields.version=Some(value),
     _=>{if fields.types.is_some() || self.bytes[start+1..end-1].contains(&b'\\'){return Err(error(EvidenceErrorCode::InvalidSource,"library declaration selector ambiguous"));}fields.types=Some(value);}
    }
   }
   let selected=depth==0 && matches!(name.as_str(),"dependencies"|"devDependencies"|"peerDependencies"|"optionalDependencies");
   let importing = depth == 0 && name == "imports" && self.aliases.is_some() && self.bytes.get(self.index) == Some(&b'{');
   if selected && self.bytes.get(self.index)!=Some(&b'{'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest dependency section must be object"));}
   if section == Some("imports") {
    if self.bytes.get(self.index) == Some(&b'"') && import_alias_name(&name) {
     let value_start = self.index;
     let (value, _, value_end) = self.string(true)?;
     self.index = value_start;
     if let Some(path) = value.filter(|path| local_import_path(path) && !self.bytes[value_start..value_end].contains(&b'\\')) {
      let aliases = self.aliases.as_mut().unwrap();
      limit(aliases.len() + self.keys.len() < self.budget.max_declarations, "manifest alias limit")?;
      aliases.push(ImportAlias { name: name.clone(), path, key_start: start, key_end: end, value_end });
     }
    }
   } else if let Some(section)=section {if self.bytes.get(self.index)!=Some(&b'"'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest dependency value must be string"));}
    let value_start=self.index; let (value,_,value_end)=self.string(true)?; self.index=value_start;
    let value=value.unwrap();
    if let Some(compare)=&mut self.compare {
     if value.starts_with("file:") {
      let raw=&self.bytes[value_start+1..value_end-1];
      if raw.contains(&b'\\') || !canonical_file_spec(&value) || package_root(&name)!=Some(name.as_str()) { compare.unsupported=true; }
      else if Sha256::digest(raw).as_slice()==compare.expected {
       if compare.found.replace(((start,end),(value_start,value_end))).is_some() {compare.unsupported=true;}
      }
     }
    }
    let unsupported_spec=["npm:","workspace:","file:","link:","git", "http:","https:","/","."].iter().any(|prefix| value.starts_with(prefix));
    if !unsupported_spec && package_root(&name)==Some(name.as_str()) {limit(self.keys.len()<self.budget.max_declarations,"manifest dependency limit")?;self.keys.push(Key{section:section.into(),name:name.clone(),start,end});}else{self.unsupported=self.unsupported.saturating_add(1);}}
   self.value(depth+1,if selected || importing{Some(name.as_str())}else{None})?;self.ws();match self.bytes.get(self.index){Some(b',')=>self.index+=1,Some(b'}')=>{self.index+=1;break},_=>return Err(error(EvidenceErrorCode::InvalidSource,"manifest object invalid"))}
  } Ok(())
 }
}

pub(super) fn extract(bytes:&[u8],budget:&ExtractionBudget)->Result<(Vec<Key>,Vec<ImportAlias>,usize),EvidenceError>{
 limit(bytes.len()<=budget.max_source_bytes,"manifest byte limit")?;
 let mut de=serde_json::Deserializer::from_slice(bytes);serde::de::IgnoredAny::deserialize(&mut de).map_err(|_|error(EvidenceErrorCode::InvalidSource,"invalid JSON manifest"))?;de.end().map_err(|_|error(EvidenceErrorCode::InvalidSource,"invalid JSON manifest"))?;
 let mut scanner=Scanner{bytes,index:0,nodes:0,budget,keys:vec![],unsupported:0,compare:None,library:None,aliases:Some(vec![])};scanner.ws();if scanner.bytes.get(scanner.index)!=Some(&b'{'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest root must be object"));}scanner.value(0,None)?;scanner.ws();if scanner.index!=bytes.len(){return Err(error(EvidenceErrorCode::InvalidSource,"manifest trailing bytes"));}Ok((scanner.keys,scanner.aliases.unwrap(),scanner.unsupported))
}

/// Compare a digest against canonical literal file dependencies without returning values.
/// JSON validation and duplicate-key checking cover the entire selected manifest.
pub(super) fn compare_file_dependency(bytes:&[u8], budget:&ExtractionBudget, expected:[u8;32]) -> Result<ManifestComparison,EvidenceError> {
 limit(bytes.len()<=budget.max_source_bytes,"manifest byte limit")?;
 let mut de=serde_json::Deserializer::from_slice(bytes);
 serde::de::IgnoredAny::deserialize(&mut de).map_err(|_|error(EvidenceErrorCode::InvalidSource,"invalid JSON manifest"))?;
 de.end().map_err(|_|error(EvidenceErrorCode::InvalidSource,"invalid JSON manifest"))?;
 let mut scanner=Scanner{bytes,index:0,nodes:0,budget,keys:vec![],unsupported:0,compare:Some(Compare{expected,found:None,unsupported:false}),library:None,aliases:None};
 scanner.ws(); if scanner.bytes.get(scanner.index)!=Some(&b'{'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest root must be object"));}
 scanner.value(0,None)?; scanner.ws(); if scanner.index!=bytes.len(){return Err(error(EvidenceErrorCode::InvalidSource,"manifest trailing bytes"));}
 let result=scanner.compare.unwrap();
 Ok(if result.unsupported {ManifestComparison::Unsupported} else if let Some((key,value))=result.found {ManifestComparison::Match{key,value}} else {ManifestComparison::NoMatch})
}


/// Read only selected package metadata in transient memory; never index values.
pub(super) fn library_fields(bytes:&[u8],budget:&ExtractionBudget)->Result<LibraryFields,EvidenceError>{
 limit(bytes.len()<=budget.max_source_bytes,"manifest byte limit")?;
 let mut de=serde_json::Deserializer::from_slice(bytes);
 serde::de::IgnoredAny::deserialize(&mut de).map_err(|_|error(EvidenceErrorCode::InvalidSource,"invalid JSON manifest"))?;
 de.end().map_err(|_|error(EvidenceErrorCode::InvalidSource,"invalid JSON manifest"))?;
 let mut scanner=Scanner{bytes,index:0,nodes:0,budget,keys:vec![],unsupported:0,compare:None,library:Some(LibraryFields::default()),aliases:None};
 scanner.ws();if scanner.bytes.get(scanner.index)!=Some(&b'{'){return Err(error(EvidenceErrorCode::InvalidSource,"manifest root must be object"));}
 scanner.value(0,None)?;scanner.ws();if scanner.index!=bytes.len(){return Err(error(EvidenceErrorCode::InvalidSource,"manifest trailing bytes"));}
 Ok(scanner.library.unwrap())
}

#[cfg(test)]
mod comparison_tests {
 use super::*;
 #[test]
 fn bounded_literal_comparison() {
  let budget=ExtractionBudget::default(); let expected: [u8;32]=Sha256::digest(b"file:../lib").into();
  let source=br#"{"dependencies":{"lib":"file:../lib","other":"^1"}}"#;
  let result=compare_file_dependency(source,&budget,expected).unwrap();
  let ManifestComparison::Match{key,value}=result else {panic!("expected match")};
  assert_eq!(&source[key.0..key.1],br#""lib""#); assert_eq!(&source[value.0..value.1],br#""file:../lib""#);
  assert_eq!(compare_file_dependency(br#"{"dependencies":{"x":"file:../other"}}"#,&budget,expected).unwrap(),ManifestComparison::NoMatch);
  assert_eq!(compare_file_dependency(br#"{"dependencies":{"x":"file:\u002e\u002e/lib"}}"#,&budget,expected).unwrap(),ManifestComparison::Unsupported);
  assert_eq!(compare_file_dependency(br#"{"dependencies":{"x":"file:../lib","y":"file:../lib"}}"#,&budget,expected).unwrap(),ManifestComparison::Unsupported);
  assert!(compare_file_dependency(br#"{"dependencies":{"x":"1","\u0078":"2"}}"#,&budget,expected).is_err());
 }
}
