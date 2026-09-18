//! Verifies that the strict `.forward` rejects targets that do not declare
//! every variant of the source.
//!
//! `SourceErrors` declares `NotFound` and `Timeout`; `NarrowTarget` declares
//! only `NotFound`. Strict-forwarding from `SourceErrors` to `NarrowTarget`
//! must be a compile error because `NarrowTarget` does not implement
//! `Has<Timeout>`.

use lore_error_set::error_set;
use lore_error_set::FfiError;
use lore_error_set::ForwardStrict;
use std::error::Error;
use std::fmt;

#[derive(Debug)]
pub struct NotFound;
impl fmt::Display for NotFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "not found")
    }
}
impl Error for NotFound {}
impl FfiError for NotFound {
    fn ffi_code(&self) -> i32 {
        1
    }
}

#[derive(Debug)]
pub struct Timeout;
impl fmt::Display for Timeout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "timeout")
    }
}
impl Error for Timeout {}
impl FfiError for Timeout {
    fn ffi_code(&self) -> i32 {
        2
    }
}

#[error_set]
pub enum SourceErrors {
    NotFound,
    Timeout,
}

#[error_set]
pub enum NarrowTarget {
    NotFound,
}

fn forward_to_narrow_target() -> Result<(), NarrowTarget> {
    let r: Result<(), SourceErrors> = Err(NotFound.into());
    // Strict forward — should fail because NarrowTarget: !Has<Timeout>.
    r.forward::<NarrowTarget>("ctx")
}

fn main() {}
