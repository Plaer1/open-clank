//! Effective app-owner source admission budgets, bounded by native invariants.
//! The existing scheduler alone activates a validated value after draining.
use serde::{Deserialize,Serialize};
use std::sync::{OnceLock,RwLock};
#[derive(Clone,Copy,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndexLimits{pub max_files:usize,pub max_source_bytes:usize,pub max_file_bytes:usize,#[serde(default="default_generation_bytes")] pub max_generation_bytes:u64}
pub const HARD_LIMITS:IndexLimits=IndexLimits{max_files:10_000,max_source_bytes:64*1024*1024,max_file_bytes:512*1024,max_generation_bytes:8*1024*1024*1024};
pub fn default_generation_bytes()->u64{2*1024*1024*1024}
impl Default for IndexLimits{fn default()->Self{Self{max_generation_bytes:default_generation_bytes(),..HARD_LIMITS}}}
impl IndexLimits{
 pub fn validate(&self)->Result<(),String>{
  if self.max_generation_bytes<4096||self.max_generation_bytes%4096!=0||self.max_generation_bytes>HARD_LIMITS.max_generation_bytes||self.max_files==0||self.max_files>HARD_LIMITS.max_files||self.max_source_bytes==0||self.max_source_bytes>HARD_LIMITS.max_source_bytes||self.max_file_bytes==0||self.max_file_bytes>HARD_LIMITS.max_file_bytes||self.max_file_bytes>self.max_source_bytes{return Err("invalid_index_limits".into());}Ok(())
 }
}
fn value()->&'static RwLock<IndexLimits>{static VALUE:OnceLock<RwLock<IndexLimits>>=OnceLock::new();VALUE.get_or_init(||RwLock::new(IndexLimits::default()))}
pub fn current()->IndexLimits{*value().read().unwrap_or_else(|p|p.into_inner())}
/// Caller must own the existing scheduler's fully drained transition.
pub(crate) fn activate(limits:IndexLimits)->Result<(),String>{limits.validate()?;*value().write().unwrap_or_else(|p|p.into_inner())=limits;Ok(())}
