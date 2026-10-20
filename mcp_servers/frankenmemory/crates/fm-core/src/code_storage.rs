//! Private, lossless, bounded derived-cache codec. Public evidence DTOs remain
//! canonical. V1 ordinary JSON is accepted only through the legacy decoder.
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
use std::io::{self, Write};

pub const CODEC_VERSION: u32 = 1;
pub const MAX_ENCODED: usize = 8 * 1024 * 1024;
pub const MAX_DECODED: usize = 64 * 1024 * 1024;
const MAX_NODES: usize = 1_000_000;
const MAX_DEPTH: usize = 128;
// Bound serde's intermediate tree too, before it allocates one Value per
// token. Packed tags/dictionary entries are not expanded evidence nodes.
const MAX_PACKED_NODES: usize = 2_000_000;

/// Imports only inspect root bindings/imports and module declarations. Function
/// locals/method bodies are deliberately not projected into another module.
pub fn module_interface(file:&super::FileEvidence)->super::FileEvidence {
    let mut retained: HashSet<&str> = HashSet::new();
    for declaration in &file.declarations {
        if declaration.scope_id == file.root_scope_id {
            retained.insert(&declaration.id);
        }
    }
    // Do not assume extraction/storage order is parent-before-child. Rust
    // module children can be selected by a qualified root module binding.
    if file.language == "rust" {
        loop {
            let modules: HashSet<_> = file.declarations.iter()
                .filter(|d| d.kind == "module" && retained.contains(d.id.as_str()))
                .map(|d| d.id.as_str()).collect();
            let before = retained.len();
            for declaration in &file.declarations {
                if declaration.lexical_parent_id.as_deref().is_some_and(|p| modules.contains(p)) {
                    retained.insert(&declaration.id);
                }
            }
            if before == retained.len() { break; }
        }
    }
    let declarations: Vec<_> = file.declarations.iter()
        .filter(|d| retained.contains(d.id.as_str())).cloned().collect();
    let bindings: Vec<_> = file.bindings.iter()
        .filter(|b| b.scope_id == file.root_scope_id).cloned().collect();
    let imports: Vec<_> = file.imports.iter()
        .filter(|i| i.scope_id == file.root_scope_id).cloned().collect();
    let diagnostics = file.diagnostics.iter().filter(|d| d.code == "conditional_import"
        && imports.iter().any(|i| d.source.as_ref() == Some(&i.source))).cloned().collect();
    let mut scopes: HashSet<&str> = declarations.iter().map(|d| d.scope_id.as_str()).collect();
    scopes.insert(&file.root_scope_id);
    loop {
        let before = scopes.len();
        for scope in &file.scopes {
            if scopes.contains(scope.id.as_str()) {
                if let Some(parent) = &scope.parent_id { scopes.insert(parent); }
            }
        }
        if before == scopes.len() { break; }
    }
    // Never clone the full occurrence/local-body corpus just to discard it.
    super::FileEvidence {
        format_version: file.format_version, checkout_id: file.checkout_id.clone(),
        file_id: file.file_id.clone(), rel_path: file.rel_path.clone(),
        source_hash: file.source_hash.clone(), source_len: file.source_len,
        language: file.language.clone(), capability: file.capability.clone(),
        producer_fingerprint: file.producer_fingerprint.clone(),
        root_scope_id: file.root_scope_id.clone(),
        scopes: file.scopes.iter().filter(|s| scopes.contains(s.id.as_str())).cloned().collect(),
        bindings, declarations, occurrences: vec![], imports,
        public_surface_fingerprint: file.public_surface_fingerprint.clone(), diagnostics,
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Packed {
    codec: u32,
    strings: Vec<String>,
    shapes: Vec<Vec<usize>>,
    value: Value,
}
fn invalid() -> String { "invalid_evidence: invalid compact partition".into() }
fn budget() -> String { "budget_exceeded: compact partition".into() }

// Serialization has a byte cap before any large canonical Value allocation.
// Both encoder and decoder have independent node/depth bounds: compressed
// size alone is not a resident-memory or expanded-output bound.
struct LimitedBytes { bytes: Vec<u8>, limit: usize, exceeded: bool }
impl Write for LimitedBytes {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            self.exceeded = true;
            return Err(io::Error::other("compact partition byte limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}
fn bounded_json<T: Serialize>(value: &T, limit: usize) -> Result<Vec<u8>, String> {
    let mut output = LimitedBytes { bytes: Vec::new(), limit, exceeded: false };
    let result = serde_json::to_writer(&mut output, value);
    if output.exceeded { return Err(budget()); }
    result.map_err(|_| invalid())?;
    Ok(output.bytes)
}

// Allocation-free lexical preflight. The actual serde parser remains the
// syntax authority; this scan only bounds tokens/depth before tree creation.
fn raw_bounds(bytes: &[u8], max_nodes: usize) -> Result<(), String> {
    let (mut nodes, mut depth, mut i) = (0usize, 0usize, 0usize);
    while i < bytes.len() {
        match bytes[i] {
            b' ' | b'\n' | b'\r' | b'\t' | b',' | b':' => i += 1,
            b'}' | b']' => { depth = depth.checked_sub(1).ok_or_else(invalid)?; i += 1; }
            byte => {
                nodes = nodes.checked_add(1).ok_or_else(budget)?;
                if nodes > max_nodes { return Err(budget()); }
                match byte {
                    b'{' | b'[' => {
                        depth += 1;
                        if depth > MAX_DEPTH { return Err(budget()); }
                        i += 1;
                    }
                    b'"' => {
                        i += 1;
                        let mut closed = false;
                        while i < bytes.len() {
                            match bytes[i] {
                                b'\\' => { i = i.checked_add(2).ok_or_else(invalid)?; }
                                b'"' => { i += 1; closed = true; break; }
                                _ => i += 1,
                            }
                        }
                        if !closed { return Err(invalid()); }
                    }
                    _ => {
                        while i < bytes.len() && !matches!(bytes[i], b',' | b':' | b']' | b'}' | b' ' | b'\n' | b'\r' | b'\t') { i += 1; }
                    }
                }
            }
        }
    }
    if depth != 0 { return Err(invalid()); }
    Ok(())
}

// Exact serialized string length without allocating escaped copies on every
// dictionary reference. This also counts repeated keys/values independently.
fn string_json_len(text: &str) -> Result<usize, String> {
    text.as_bytes().iter().try_fold(2usize, |total, byte| {
        total.checked_add(match byte { b'"' | b'\\' | b'\n' | b'\r' | b'\t' | 8 | 12 => 2, 0..=31 => 6, _ => 1 }).ok_or_else(budget)
    })
}

pub fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>, String> {
    let raw = bounded_json(value, MAX_DECODED)?;
    raw_bounds(&raw, MAX_NODES)?;
    let value: Value = serde_json::from_slice(&raw).map_err(|_| invalid())?;
    drop(raw);
    let mut strings = Vec::new();
    let mut string_ids = BTreeMap::new();
    let mut shapes = Vec::new();
    let mut shape_ids = BTreeMap::new();
    fn intern(text: String, strings: &mut Vec<String>, ids: &mut BTreeMap<String,usize>) -> usize {
        if let Some(i)=ids.get(&text) {return *i;}
        let i=strings.len(); ids.insert(text.clone(),i); strings.push(text); i
    }
    fn pack(value: Value, depth:usize, nodes:&mut usize, decoded:&mut usize,
        strings:&mut Vec<String>, ids:&mut BTreeMap<String,usize>,
        shapes:&mut Vec<Vec<usize>>, shape_ids:&mut BTreeMap<Vec<usize>,usize>) -> Result<Value,String> {
        *nodes=nodes.checked_add(1).ok_or_else(budget)?;
        if depth>MAX_DEPTH || *nodes>MAX_NODES{return Err(budget());}
        Ok(match value {
            Value::String(text)=>{
                *decoded=decoded.checked_add(text.len()+2).ok_or_else(budget)?;
                if *decoded>MAX_DECODED{return Err(budget());}
                json!([-2,intern(text,strings,ids)])
            },
            Value::Object(fields)=>{
                let keys:Vec<_>=fields.keys().cloned().map(|s|intern(s,strings,ids)).collect();
                let n=shapes.len();let index=*shape_ids.entry(keys.clone()).or_insert_with(||{shapes.push(keys);n});
                let mut values=vec![json!(-1),json!(index)];
                for (_,v) in fields {values.push(pack(v,depth+1,nodes,decoded,strings,ids,shapes,shape_ids)?);}
                Value::Array(values)
            },
            Value::Array(items)=>{
                let mut values=vec![json!(-3)];
                for v in items {values.push(pack(v,depth+1,nodes,decoded,strings,ids,shapes,shape_ids)?);}
                Value::Array(values)
            },
            x=>x
        })
    }
    let value=pack(value,0,&mut 0,&mut 0,&mut strings,&mut string_ids,&mut shapes,&mut shape_ids)?;
    let packed = Packed{codec:CODEC_VERSION,strings,shapes,value};
    inspect(&packed.value, &packed, 0, &mut 0, &mut 0)?;
    let bytes = bounded_json(&packed, MAX_ENCODED)?;
    raw_bounds(&bytes, MAX_PACKED_NODES)?;
    Ok(bytes)
}

// Validate the entire expansion BEFORE cloning dictionary strings or
// allocating expanded records. Count all occurrences, not unique strings.
fn inspect(value:&Value,p:&Packed,depth:usize,nodes:&mut usize,size:&mut usize)->Result<(),String>{
        *nodes=nodes.checked_add(1).ok_or_else(budget)?;
        *size=size.checked_add(2).ok_or_else(budget)?;
        if depth>MAX_DEPTH || *nodes>MAX_NODES || *size>MAX_DECODED{return Err(budget());}
        if let Value::Array(a)=value {
            let tag=a.first().and_then(Value::as_i64).ok_or_else(invalid)?;
            match tag {
                -2=>{if a.len()!=2{return Err(invalid());}let id=a[1].as_u64().and_then(|v|usize::try_from(v).ok()).ok_or_else(invalid)?;
                    let s=p.strings.get(id).ok_or_else(invalid)?;
                    *size=size.checked_add(string_json_len(s)?).ok_or_else(budget)?;
                },
                -1=>{let id=a.get(1).and_then(Value::as_u64).and_then(|v|usize::try_from(v).ok()).ok_or_else(invalid)?;
                    let keys=p.shapes.get(id).ok_or_else(invalid)?;if a.len()!=keys.len()+2{return Err(invalid());}
                    let mut unique=std::collections::BTreeSet::new();
                    for (key,v) in keys.iter().zip(&a[2..]) {let name=p.strings.get(*key).ok_or_else(invalid)?;
                        if !unique.insert(name){return Err(invalid());}
                        *size=size.checked_add(string_json_len(name)?+2).ok_or_else(budget)?;
                        inspect(v,p,depth+1,nodes,size)?;}
                },
                -3=>for v in &a[1..]{inspect(v,p,depth+1,nodes,size)?;},
                _=>return Err(invalid())
            }
        } else if value.is_object() || value.is_string(){return Err(invalid());}
        else {*size=size.checked_add(value.to_string().len()).ok_or_else(budget)?;}
        if *size>MAX_DECODED{return Err(budget());} Ok(())
}

pub fn decode<T: DeserializeOwned>(bytes: &[u8]) -> Result<T,String> {
    if bytes.len()>MAX_ENCODED {return Err(budget());}
    raw_bounds(bytes, MAX_PACKED_NODES)?;
    let packed:Packed=serde_json::from_slice(bytes).map_err(|_|invalid())?;
    if packed.codec!=CODEC_VERSION || packed.strings.len()>MAX_NODES || packed.shapes.len()>MAX_NODES{return Err(invalid());}
    inspect(&packed.value,&packed,0,&mut 0,&mut 0)?;
    fn expand(value:&Value,p:&Packed)->Value {
        if let Value::Array(a)=value {match a[0].as_i64().unwrap(){
            -2=>Value::String(p.strings[a[1].as_u64().unwrap() as usize].clone()),
            -1=>Value::Object(p.shapes[a[1].as_u64().unwrap() as usize].iter().zip(&a[2..]).map(|(k,v)|(p.strings[*k].clone(),expand(v,p))).collect()),
            -3=>Value::Array(a[1..].iter().map(|v|expand(v,p)).collect()),
            _=>unreachable!()
        }}else{value.clone()}
    }
    serde_json::from_value(expand(&packed.value,&packed)).map_err(|_|invalid())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn compact_roundtrip_preserves_nested_types_and_literals(){
        let v=json!({"long repeated key":["same","same",null,[-1,-2,-3],{"x":true}],"other":"same"});
        assert_eq!(decode::<Value>(&encode(&v).unwrap()).unwrap(),v);
    }
    #[test] fn rejects_unknown_reference_shape_and_version(){
        for s in [r#"{"codec":2,"strings":[],"shapes":[],"value":null}"#,
            r#"{"codec":1,"strings":[],"shapes":[],"value":[-2,0]}"#,
            r#"{"codec":1,"strings":["x"],"shapes":[[0,0]],"value":[-1,0,1,2]}"#]{assert!(decode::<Value>(s.as_bytes()).is_err());}
    }
    #[test] fn rejects_expansion_bomb_before_expanding(){
        let packed=Packed{codec:1,strings:vec!["x".repeat(1024*1024)],shapes:vec![],value:Value::Array(std::iter::once(json!(-3)).chain((0..65).map(|_|json!([-2,0]))).collect())};
        assert!(decode::<Value>(&serde_json::to_vec(&packed).unwrap()).unwrap_err().starts_with("budget_exceeded:"));
    }
    #[test] fn escaping_keys_and_numeric_extremes_roundtrip(){
        let v=json!({"\u{0}\n\"\\😀":[u64::MAX,i64::MIN,-0.0,1.25,true,false,"\u{1}\n\"\\😀"]});
        assert_eq!(decode::<Value>(&encode(&v).unwrap()).unwrap(),v);
        assert_eq!(string_json_len("\u{0}\n\"\\😀").unwrap(), serde_json::to_vec("\u{0}\n\"\\😀").unwrap().len());
    }
    #[test] fn rejects_malformed_tags_and_raw_json_in_packed_values(){
        for value in [json!([]),json!([-1]),json!([-2]),json!([-2,-1]),json!([-2,0,1]),json!([-4]),json!({"raw":true}),json!("raw")] {
            let bytes=serde_json::to_vec(&Packed{codec:1,strings:vec!["x".into()],shapes:vec![],value}).unwrap();
            assert!(decode::<Value>(&bytes).is_err());
        }
        assert!(decode::<Value>(br#"{"codec":1,"strings":[],"shapes":[],"value":null,"extra":0}"#).is_err());
        assert!(decode::<Value>(br#"{"codec":1,"strings":[],"shapes":[],"value":null} true"#).is_err());
    }
    #[test] fn raw_preflight_bounds_tokens_and_depth_before_parse(){
        assert!(raw_bounds(br#"[null,null,null]"#,3).unwrap_err().starts_with("budget_exceeded:"));
        let deep=format!("{}null{}", "[".repeat(MAX_DEPTH+1), "]".repeat(MAX_DEPTH+1));
        assert!(raw_bounds(deep.as_bytes(),MAX_PACKED_NODES).unwrap_err().starts_with("budget_exceeded:"));
        assert!(raw_bounds(br#"["unterminated]"#,100).is_err());
        assert!(raw_bounds(br#"["escaped\\\"text"]"#,100).is_ok());
    }
    #[test] fn key_expansion_bomb_and_writer_limit_are_independent(){
        let p=Packed{codec:1,strings:vec!["x".repeat(1024*1024)],shapes:vec![vec![0]],
            value:Value::Array(std::iter::once(json!(-3)).chain((0..65).map(|_|json!([-1,0,null]))).collect())};
        assert!(decode::<Value>(&serde_json::to_vec(&p).unwrap()).unwrap_err().starts_with("budget_exceeded:"));
        assert_eq!(bounded_json(&"\n",4).unwrap().as_slice(),br#""\n""#);
        assert!(bounded_json(&"\n",3).unwrap_err().starts_with("budget_exceeded:"));
    }
    fn extracted(path:&str, source:&str)->super::super::FileEvidence {
        super::super::extract_evidence("checkout-test",path,source.as_bytes(),&super::super::ExtractionBudget::default()).unwrap()
    }
    #[test] fn interface_retains_conditional_alias_semantics_without_local_bodies(){
        use super::super::*;
        let dep=extracted("dep.py","def target():\n    local = 1\n    return local\n");
        let bridge=extracted("bridge.py","if enabled:\n    from dep import target\ndef hidden():\n    from dep import target as local\n    local()\n");
        let caller=extracted("main.py","from bridge import target\ntarget()\n");
        let projected=module_interface(&bridge);
        assert!(projected.occurrences.is_empty());
        assert!(projected.bindings.iter().all(|b|b.scope_id==bridge.root_scope_id));
        assert!(projected.imports.iter().all(|i|i.scope_id==bridge.root_scope_id));
        assert!(projected.diagnostics.iter().any(|d|d.code=="conditional_import"));
        let input=|files|CorpusInput{checkout_id:"checkout-test".into(),files,unavailable_files:vec![]};
        let full=resolve_evidence(&input(vec![caller.clone(),bridge.clone(),dep.clone()]),&ResolutionBudget::default()).unwrap();
        let compact=resolve_evidence(&input(vec![caller.clone(),projected,module_interface(&dep)]),&ResolutionBudget::default()).unwrap();
        for occurrence in &caller.occurrences {
            assert_eq!(full.resolutions.iter().find(|r|r.occurrence_id==occurrence.id),compact.resolutions.iter().find(|r|r.occurrence_id==occurrence.id));
        }
    }
    #[test] fn rust_nested_modules_survive_reordered_declarations_and_keep_scope_ancestors(){
        let mut file=extracted("lib.rs","pub mod outer { pub mod inner { pub fn target() { let local = 1; } } }\n");
        file.declarations.reverse();
        let projected=module_interface(&file);
        assert!(projected.declarations.iter().any(|d|d.name=="target"));
        assert!(projected.bindings.iter().all(|b|b.scope_id==file.root_scope_id));
        for declaration in &projected.declarations {
            assert!(projected.scopes.iter().any(|s|s.id==declaration.scope_id));
            if let Some(parent)=&declaration.lexical_parent_id {assert!(projected.declarations.iter().any(|d|&d.id==parent));}
        }
        for scope in &projected.scopes {
            if let Some(parent)=&scope.parent_id {assert!(projected.scopes.iter().any(|s|&s.id==parent));}
            if let Some(owner)=&scope.declaration_id {assert!(projected.declarations.iter().any(|d|&d.id==owner));}
        }
        assert_eq!(decode::<super::super::FileEvidence>(&encode(&projected).unwrap()).unwrap(),projected);
    }
}
