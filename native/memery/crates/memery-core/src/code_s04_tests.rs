use super::*;

#[test]
fn streamed_compatibility_refs_equal_legacy_order_dedup_and_errors(){
 for(lang,source)in [
  (Lang::Python,"import os\nimport os\nfrom pathlib import Path\ndef caller():\n    first()\n    first()\n    second()\n    first()\n"),
  (Lang::Rust,"use std::io;\nuse std::io;\nfn caller(){ first(); first(); item.second(); third::first(); }\n"),
  (Lang::TypeScript,"import {one} from './one'; import {two} from './two'; export function caller(){ one(); one(); two(); one(); }\n"),
  (Lang::Tsx,"import React from 'react'; export function App(){ click(); click(); return <div onClick={click}/>; }\n"),
 ]{
  let old=extract_file(lang,source).unwrap();let expected:Vec<_>=old.imports.into_iter().map(|s|(CompatibilityReference::Import,s)).chain(old.calls.into_iter().map(|s|(CompatibilityReference::Call,s))).collect();let mut actual=Vec::new();stream_compatibility_references(lang,source,|kind,text|{actual.push((kind,text.to_owned()));Ok(())}).unwrap();assert_eq!(actual,expected,"same legacy source query and per-kind adjacent dedup");
 }
 for(lang,source)in [(Lang::Python,"def broken(:\n"),(Lang::Rust,"fn broken( {"),(Lang::TypeScript,"function broken( {"),(Lang::Tsx,"function App( { <>")]{assert!(extract_file(lang,source).is_err());assert!(stream_compatibility_references(lang,source,|_,_|panic!("no partial callback from syntax failure")).is_err());}
 let mut accepted=0;assert!(stream_compatibility_references(Lang::Python,"first()\nsecond()\n",|_,_|{accepted+=1;Err("budget_exceeded: intentional stop".into())}).is_err());assert_eq!(accepted,1);
}

#[test]
fn graph_output_counter_is_byte_exact_bounded_before_append(){
 let value="é".repeat(100);let actual=serde_json::to_vec(&value).unwrap().len();let mut budget=GraphOutputBudget::new(64+actual+1).unwrap();budget.account(&value).unwrap();assert_eq!(budget.used,budget.limit);assert!(budget.account(&value).is_err());assert_eq!(budget.used,budget.limit);
 let mut too_small=GraphOutputBudget::new(64+100).unwrap();assert!(too_small.account(&value).is_err());assert_eq!(too_small.used,64,"failure does not admit the record");
}

#[test]
fn bounded_graph_preserves_complete_record_properties_and_rejects_without_partial_result(){
 let root=std::env::temp_dir().join(format!("fm-s04-stream-{}",uuid::Uuid::new_v4()));std::fs::create_dir(&root).unwrap();let root=root.canonicalize().unwrap();let path=root.join("caller.py");std::fs::write(&path,"from lib import target\ndef caller():\n    target()\n    target()\n    other()\n    target()\n").unwrap();let identity=directory_identity(&root).unwrap();let acquired=acquire_source_pinned(&root,&path,&identity).unwrap();let budget=crate::code_evidence::ExtractionBudget::default();
 let indexed=index_acquired_graph_budget("checkout",&root,&path,&acquired,&budget,8*1024*1024).unwrap();let baseline=index_acquired_budget("checkout",&root,&path,&acquired,&budget).unwrap();assert_eq!(indexed.evidence,baseline.evidence);assert_eq!(serde_json::to_value(&indexed.upsert).unwrap(),serde_json::to_value(&baseline.upsert).unwrap());
 let compatibility=extract_file(Lang::Python,std::str::from_utf8(&acquired.bytes).unwrap()).unwrap();let imports:Vec<_>=indexed.upsert.edges.iter().filter(|e|e.tag=="imports").map(|e|e.dst.name.clone()).collect();let calls:Vec<_>=indexed.upsert.edges.iter().filter(|e|e.tag=="calls").map(|e|e.dst.name.clone()).collect();assert_eq!(imports,compatibility.imports.iter().map(|s|format!("checkout::{s}")).collect::<Vec<_>>());assert_eq!(calls,compatibility.calls.iter().map(|s|format!("checkout::{s}")).collect::<Vec<_>>());
 assert!(indexed.upsert.edges.iter().filter(|e|e.tag=="calls").all(|e|e.trust==Some(0)&&e.src.kind=="file"&&e.dst.kind=="callable"));assert!(indexed.upsert.edges.iter().filter(|e|e.tag=="imports").all(|e|e.trust==Some(3)&&e.dst.kind=="module"));assert!(matches!(index_acquired_graph_budget("checkout",&root,&path,&acquired,&budget,64),Err(e)if e.starts_with("budget_exceeded:")));assert!(serde_json::to_vec(&indexed.upsert).unwrap().len()<=8*1024*1024);
 assert!(root.file_name().unwrap().to_string_lossy().starts_with("fm-s04-stream-"));std::fs::remove_dir_all(root).unwrap();
}
