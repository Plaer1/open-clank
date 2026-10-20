// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for `ResultExt::map_matched_err`.
//!
//! `map_matched_err` combines `try_match` + closure mapping in a single call:
//! - Handleable errors are passed to the closure for conversion
//! - Internal errors are propagated with the context string added to the trace

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;
use lore_error_set::ResultExt;

// ---------------------------------------------------------------------------
// Discrete error types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct NotFound {
    resource: String,
}

impl fmt::Display for NotFound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "not found: {}", self.resource)
    }
}

impl Error for NotFound {}

impl FfiError for NotFound {
    fn ffi_code(&self) -> i32 {
        10
    }
}

#[derive(Debug, Clone)]
pub struct Timeout {
    duration_ms: u64,
}

impl fmt::Display for Timeout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "timeout after {}ms", self.duration_ms)
    }
}

impl Error for Timeout {}

impl FfiError for Timeout {
    fn ffi_code(&self) -> i32 {
        11
    }
}

#[derive(Debug, Clone)]
pub struct InvalidPath {
    path: String,
}

impl fmt::Display for InvalidPath {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid path: {}", self.path)
    }
}

impl Error for InvalidPath {}

impl FfiError for InvalidPath {
    fn ffi_code(&self) -> i32 {
        12
    }
}

// ---------------------------------------------------------------------------
// Error sets
// ---------------------------------------------------------------------------

#[error_set]
pub enum SourceErrors {
    NotFound,
    Timeout,
}

#[error_set]
pub enum TargetErrors {
    NotFound,
    InvalidPath,
}

// ---------------------------------------------------------------------------
// Helper operations
// ---------------------------------------------------------------------------

fn source_success() -> Result<String, SourceErrors> {
    Ok("value".into())
}

fn source_not_found() -> Result<String, SourceErrors> {
    Err(NotFound {
        resource: "doc".into(),
    }
    .into())
}

fn source_timeout() -> Result<String, SourceErrors> {
    Err(Timeout { duration_ms: 2000 }.into())
}

fn source_internal() -> Result<String, SourceErrors> {
    let io_err = std::io::Error::other("disk failure");
    Err(SourceErrors::internal_with_context(
        io_err,
        "source context",
    ))
}

// ---------------------------------------------------------------------------
// Tests: success passthrough
// ---------------------------------------------------------------------------

#[test]
fn success_passes_through() {
    let result: Result<String, TargetErrors> =
        source_success().map_matched_err("mapping", |m| match m {
            MatchedSourceErrors::NotFound(_) => TargetErrors::from(InvalidPath {
                path: "fallback".into(),
            }),
            MatchedSourceErrors::Timeout(_) => TargetErrors::from(InvalidPath {
                path: "timeout-fallback".into(),
            }),
        });

    assert_eq!(result.unwrap(), "value");
}

// ---------------------------------------------------------------------------
// Tests: handleable error mapped by closure
// ---------------------------------------------------------------------------

#[test]
fn handleable_error_mapped_by_closure() {
    let result: Result<String, TargetErrors> =
        source_not_found().map_matched_err("mapping", |m| match m {
            MatchedSourceErrors::NotFound(traced) => TargetErrors::from(InvalidPath {
                path: format!("mapped-{}", traced.resource),
            }),
            MatchedSourceErrors::Timeout(_) => TargetErrors::from(InvalidPath {
                path: "timeout".into(),
            }),
        });

    let err = result.unwrap_err();
    assert!(err.is_invalid_path());
    let inner = err.as_invalid_path().unwrap();
    assert_eq!(inner.path, "mapped-doc");
}

#[test]
fn different_handleable_variant_mapped_by_closure() {
    let result: Result<String, TargetErrors> =
        source_timeout().map_matched_err("mapping", |m| match m {
            MatchedSourceErrors::NotFound(_) => TargetErrors::from(InvalidPath {
                path: "not-found".into(),
            }),
            MatchedSourceErrors::Timeout(traced) => TargetErrors::from(InvalidPath {
                path: format!("timed-out-{}ms", traced.duration_ms),
            }),
        });

    let err = result.unwrap_err();
    assert!(err.is_invalid_path());
    let inner = err.as_invalid_path().unwrap();
    assert_eq!(inner.path, "timed-out-2000ms");
}

// ---------------------------------------------------------------------------
// Tests: Internal errors propagate with context
// ---------------------------------------------------------------------------

#[test]
fn internal_error_propagates_with_context() {
    let result: Result<String, TargetErrors> =
        source_internal().map_matched_err("propagation site", |m| match m {
            MatchedSourceErrors::NotFound(_) | MatchedSourceErrors::Timeout(_) => {
                TargetErrors::from(InvalidPath {
                    path: "fallback".into(),
                })
            }
        });

    let err = result.unwrap_err();
    assert!(err.is_internal());
    // Display prepends the most-recent context — the map_matched_err hop.
    assert_eq!(err.to_string(), "propagation site: disk failure");
    // Both contexts appear on the trace.
    let trace = err.trace();
    let contexts: Vec<Option<&str>> = trace.locations().iter().map(|l| l.context()).collect();
    assert!(
        contexts.contains(&Some("source context")),
        "upstream construction context should appear on trace; got {contexts:?}"
    );
    assert!(
        contexts.contains(&Some("propagation site")),
        "hop context should appear on trace; got {contexts:?}"
    );
}

#[test]
fn internal_error_preserves_source_chain() {
    let result: Result<String, TargetErrors> =
        source_internal().map_matched_err("mapping context", |m| match m {
            MatchedSourceErrors::NotFound(_) | MatchedSourceErrors::Timeout(_) => {
                TargetErrors::from(InvalidPath {
                    path: "fallback".into(),
                })
            }
        });

    let err = result.unwrap_err();
    // Display prepends the most-recent context from the trace.
    assert_eq!(err.to_string(), "mapping context: disk failure");
    // source() skips directly to io::Error — no nested Internal.
    let source = err.source().expect("should have source chain");
    assert!(
        source.downcast_ref::<std::io::Error>().is_some(),
        "source should be io::Error directly, not a nested Internal"
    );
}

// ---------------------------------------------------------------------------
// Tests: closure is not called on success or Internal
// ---------------------------------------------------------------------------

#[test]
fn closure_not_called_on_success() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);
    CALLED.store(false, Ordering::SeqCst);

    let _: Result<String, TargetErrors> = source_success().map_matched_err("ctx", |_m| {
        CALLED.store(true, Ordering::SeqCst);
        TargetErrors::from(InvalidPath {
            path: "should not happen".into(),
        })
    });

    assert!(!CALLED.load(Ordering::SeqCst));
}

#[test]
fn closure_not_called_on_internal() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);
    CALLED.store(false, Ordering::SeqCst);

    let _: Result<String, TargetErrors> = source_internal().map_matched_err("ctx", |_m| {
        CALLED.store(true, Ordering::SeqCst);
        TargetErrors::from(InvalidPath {
            path: "should not happen".into(),
        })
    });

    assert!(!CALLED.load(Ordering::SeqCst));
}
