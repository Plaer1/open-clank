//! Effective app-owner source admission budgets, bounded by native invariants.
//! The existing scheduler alone activates a validated value after draining.
use serde::{Deserialize,Serialize};
use std::sync::{OnceLock,RwLock};
#[derive(Clone,Copy,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndexLimits{pub max_files:usize,pub max_source_bytes:usize,pub max_file_bytes:usize,#[serde(default="default_generation_bytes")] pub max_generation_bytes:u64}
pub const MAX_INDEXED_FILES:usize=75_000;
const LEGACY_MAX_INDEXED_FILES:usize=10_000;
const MIB:usize=1024*1024;
const GIB:u64=1024*1024*1024;
pub const HARD_LIMITS:IndexLimits=IndexLimits{
    max_files:MAX_INDEXED_FILES,max_source_bytes:1024*MIB,max_file_bytes:16*MIB,max_generation_bytes:32*GIB,
};
pub fn default_generation_bytes()->u64{8*GIB}
impl Default for IndexLimits {
    fn default()->Self {
        Self{max_files:MAX_INDEXED_FILES,max_source_bytes:256*MIB,max_file_bytes:4*MIB,max_generation_bytes:default_generation_bytes()}
    }
}
impl IndexLimits{
 pub(crate) fn is_legacy_default(self)->bool{
  matches!(self.max_files, LEGACY_MAX_INDEXED_FILES | MAX_INDEXED_FILES)
   && self.max_source_bytes==64*MIB && self.max_file_bytes==512*1024 && self.max_generation_bytes==2*GIB
 }
 pub fn validate(&self)->Result<(),String>{
  if self.max_generation_bytes<4096||self.max_generation_bytes%4096!=0||self.max_generation_bytes>HARD_LIMITS.max_generation_bytes||self.max_files==0||self.max_files>HARD_LIMITS.max_files||self.max_source_bytes==0||self.max_source_bytes>HARD_LIMITS.max_source_bytes||self.max_file_bytes==0||self.max_file_bytes>HARD_LIMITS.max_file_bytes||self.max_file_bytes>self.max_source_bytes{return Err("invalid_index_limits".into());}Ok(())
 }
}
fn value()->&'static RwLock<IndexLimits>{static VALUE:OnceLock<RwLock<IndexLimits>>=OnceLock::new();VALUE.get_or_init(||RwLock::new(IndexLimits::default()))}
pub fn current()->IndexLimits{*value().read().unwrap_or_else(|p|p.into_inner())}
/// Caller must own the existing scheduler's fully drained transition.
pub(crate) fn activate(limits:IndexLimits)->Result<(),String>{limits.validate()?;*value().write().unwrap_or_else(|p|p.into_inner())=limits;Ok(())}
