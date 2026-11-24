//! Closed deferred-count generic stream reader. No provisional output is success.
use crate::{codec, decoder::{self, DecodeError}, parser::{self, Value, MAX_BYTES}};
use std::collections::HashSet;

const MAX_ROOT: usize = 256;
const MAX_FIELDS: usize = 256;
const MAX_VALUES: usize = 16_384;

fn write_key(out:&mut String, name:&str)->Result<(),DecodeError>{
    out.push_str(&codec::stream_key(name).map_err(|_|DecodeError::Invalid)?); Ok(())
}
fn write_scalar(out:&mut String, value:&Value)->Result<(),DecodeError>{
    out.push_str(&codec::stream_scalar(value).map_err(|_|DecodeError::Invalid)?); Ok(())
}
fn count(total:&mut usize, add:usize)->Result<(),DecodeError>{
    *total=total.checked_add(add).filter(|n|*n<=MAX_VALUES).ok_or(DecodeError::TooManyValues)?; Ok(())
}

/// Verify a complete stream and return the bounded canonical JSON value.
/// Table field metadata is retained until canonical frame validation, including empty tables.
pub fn decode_stream(input:&[u8])->Result<Vec<u8>,DecodeError>{
    if input.len()>MAX_BYTES {return Err(DecodeError::TooLarge)}
    let frame=std::str::from_utf8(input).map_err(|_|DecodeError::Invalid)?;
    let rest=frame.strip_prefix("GCF profile=generic\n").ok_or(DecodeError::Invalid)?;
    if !rest.is_empty() && !rest.ends_with('\n') {return Err(DecodeError::Invalid)}
    let lines:Vec<&str>=if rest.is_empty(){Vec::new()}else{rest[..rest.len()-1].split('\n').collect()};
    let mut at=0usize; let mut values=1usize; let mut names=HashSet::new();
    let mut pairs:Vec<(String,Value)>=Vec::new(); let mut table_counts=Vec::new();
    let mut canonical=String::from("GCF profile=generic\n");
    while at<lines.len() && !lines[at].starts_with("##! ") {
        let line=lines[at]; at+=1;
        if pairs.len()>=MAX_ROOT{return Err(DecodeError::TooManyValues)}
        if let Some(header)=line.strip_prefix("## ") {
            let pos=decoder::unquoted_bracket(header).ok_or(DecodeError::Invalid)?;
            let prefix=header[..pos].strip_suffix(' ').ok_or(DecodeError::Invalid)?;
            let name=decoder::key(prefix)?;
            if name.is_empty() || !names.insert(name.clone()){return Err(DecodeError::Invalid)}
            let fields_raw=header[pos..].strip_prefix("[?]{").and_then(|s|s.strip_suffix('}')).ok_or(DecodeError::Invalid)?;
            let encoded_fields=decoder::split_unquoted(fields_raw,b',')?;
            if encoded_fields.is_empty() || encoded_fields.len()>MAX_FIELDS{return Err(DecodeError::Invalid)}
            let mut fields=Vec::with_capacity(encoded_fields.len()); let mut unique=HashSet::new();
            for f in encoded_fields {let name=decoder::key(f)?; if name.is_empty() || name.contains('>') || !unique.insert(name.clone()){return Err(DecodeError::Invalid)} fields.push(name)}
            canonical.push_str("## ");write_key(&mut canonical,&name)?;canonical.push_str(" [?]{");
            for (i,f) in fields.iter().enumerate(){if i>0{canonical.push(',')}write_key(&mut canonical,f)?}canonical.push_str("}\n");
            count(&mut values,1)?; let mut rows=Vec::new();
            while at<lines.len(){let row=lines[at];
                if row.starts_with("## ") || row.starts_with("##! ") || decoder::split_unquoted(row,b'=')?.len()>1 {break}
                at+=1;let cells=decoder::split_unquoted(row,b'|')?;
                if cells.len()!=fields.len() || rows.len()>=MAX_VALUES{return Err(DecodeError::Invalid)}
                count(&mut values,1+cells.len())?;let mut members=Vec::with_capacity(fields.len());
                for (i,(field,cell)) in fields.iter().zip(cells.iter()).enumerate(){
                    let v=decoder::atom(cell)?;if i>0{canonical.push('|')}write_scalar(&mut canonical,&v)?;
                    members.push((field.clone(),v));
                }
                canonical.push('\n'); rows.push(Value::Object(members));
            }
            table_counts.push(rows.len());pairs.push((name,Value::Array(rows)));
        }else{
            let parts=decoder::split_unquoted(line,b'=')?;
            if parts.len()!=2{return Err(DecodeError::Invalid)}
            let name=decoder::key(parts[0])?;if name.is_empty() || !names.insert(name.clone()){return Err(DecodeError::Invalid)}
            let value=decoder::atom(parts[1])?;count(&mut values,1)?;
            write_key(&mut canonical,&name)?;canonical.push('=');write_scalar(&mut canonical,&value)?;canonical.push('\n');
            pairs.push((name,value));
        }
    }
    if !table_counts.is_empty(){
        let summary=lines.get(at).ok_or(DecodeError::Invalid)?;at+=1;
        let expected=table_counts.iter().map(usize::to_string).collect::<Vec<_>>().join(",");
        if *summary != format!("##! summary counts={}",expected){return Err(DecodeError::Invalid)}
        canonical.push_str("##! summary counts=");canonical.push_str(&expected);canonical.push('\n');
    }
    if at!=lines.len() || canonical.as_bytes()!=input{return Err(DecodeError::Invalid)}
    let value=Value::Object(pairs);
    let json=codec::canonical_json(&value).map_err(|_|DecodeError::TooLarge)?;
    match parser::parse(&json){
        Ok(parsed) if parsed==value=>Ok(json),
        Err(parser::Reject::TooLarge)=>Err(DecodeError::TooLarge),
        Err(parser::Reject::TooDeep)=>Err(DecodeError::TooDeep),
        Err(parser::Reject::TooManyValues)=>Err(DecodeError::TooManyValues),
        _=>Err(DecodeError::Invalid),
    }
}
