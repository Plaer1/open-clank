// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for strict `.forward` and full propagation chains.
//!
//! Covers spec acceptance tests:
//! - #15: Full propagation chain `.try_match("a")?.map_err(|m| m.forward("b"))?`
//! - #17: Lazy context — closure only called on error path
//!
//! Strict forward requires the target to declare every variant of the source.
//! `TargetErrors` below is a superset of `SourceErrors`, so every variant maps
//! directly and no source error ever collapses to `Target::Internal` solely
//! from a missing variant.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::prelude::*;
use lore_error_set::FfiError;

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

#[derive(Debug)]
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

#[derive(Debug)]
pub struct RateLimit;

impl fmt::Display for RateLimit {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "rate limited")
    }
}

impl Error for RateLimit {}

impl FfiError for RateLimit {
    fn ffi_code(&self) -> i32 {
        12
    }
}

// ---------------------------------------------------------------------------
// Source error set (has NotFound, Timeout, RateLimit)
// ---------------------------------------------------------------------------

#[error_set]
pub enum SourceErrors {
    NotFound,
    Timeout,
    RateLimit,
}

// ---------------------------------------------------------------------------
// Target error set — superset of SourceErrors so strict forward type-checks.
// ---------------------------------------------------------------------------

#[error_set]
pub enum TargetErrors {
    NotFound,
    Timeout,
    RateLimit,
}

// ---------------------------------------------------------------------------
// Acceptance test #15: Full propagation chain
// .try_match("a")?.map_err(|m| m.forward("b"))?
// ---------------------------------------------------------------------------

/// Source operation that succeeds.
fn source_success() -> Result<String, SourceErrors> {
    Ok("hello".into())
}

/// Source operation that returns `NotFound`.
fn source_not_found() -> Result<String, SourceErrors> {
    Err(NotFound {
        resource: "doc".into(),
    }
    .into())
}

/// Source operation that returns Timeout.
fn source_timeout() -> Result<String, SourceErrors> {
    Err(Timeout { duration_ms: 3000 }.into())
}

/// Source operation that returns Internal.
fn source_internal() -> Result<String, SourceErrors> {
    let io_err = std::io::Error::other("disk failure");
    Err(SourceErrors::internal_with_context(
        io_err,
        "source internal",
    ))
}

#[test]
fn full_chain_success_returns_value() {
    fn caller() -> Result<String, TargetErrors> {
        let value = source_success()
            .try_match("step a")?
            .map_err(|m| m.forward::<TargetErrors>("step b"))?;
        Ok(value)
    }

    let result = caller();
    assert_eq!(result.unwrap(), "hello");
}

#[test]
fn full_chain_not_found_maps_directly_to_target() {
    fn caller() -> Result<String, TargetErrors> {
        let value = source_not_found()
            .try_match("step a")?
            .map_err(|m| m.forward::<TargetErrors>("step b"))?;
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    assert!(err.is_not_found());
    assert_eq!(err.to_string(), "not found: doc");
}

#[test]
fn full_chain_timeout_maps_directly_to_target() {
    fn caller() -> Result<String, TargetErrors> {
        let value = source_timeout()
            .try_match("step a")?
            .map_err(|m| m.forward::<TargetErrors>("step b"))?;
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    assert!(err.is_timeout());
    assert_eq!(err.to_string(), "timeout after 3000ms");
}

#[test]
fn full_chain_internal_propagates_with_try_match_context() {
    fn caller() -> Result<String, TargetErrors> {
        let value = source_internal()
            .try_match("step a")?
            .map_err(|m| m.forward::<TargetErrors>("step b"))?;
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    // Internal should propagate from try_match, not reach forward.
    // try_match("step a") encounters Internal, adopts it as-is (no nesting),
    // and records "step a" as a context-bearing trace entry. The ? then
    // converts Traced<Internal> -> TargetErrors::Internal via
    // From<Traced<Internal>> without re-tracing.
    assert!(err.is_internal());
    // Display walks newest-first, finds the try_match hop context.
    assert_eq!(err.to_string(), "step a: disk failure");
    // The source is the original io::Error directly — no nested Internal.
    let source = err.source().expect("should have source");
    assert!(
        source.downcast_ref::<std::io::Error>().is_some(),
        "source should be io::Error, not a nested Internal"
    );
    // Both the upstream construction context AND the try_match hop are on
    // the trace.
    let trace = err.trace();
    let contexts: Vec<Option<&str>> = trace.locations().iter().map(|l| l.context()).collect();
    assert!(
        contexts.contains(&Some("source internal")),
        "upstream construction context should appear on trace; got {contexts:?}"
    );
    assert!(
        contexts.contains(&Some("step a")),
        "try_match hop context should appear on trace; got {contexts:?}"
    );
}

// ---------------------------------------------------------------------------
// ForwardStrict::forward directly on Result (without try_match)
// ---------------------------------------------------------------------------

#[test]
fn forward_direct_mapping() {
    let result: Result<String, TargetErrors> = source_not_found().forward("forwarding directly");

    let err = result.unwrap_err();
    assert!(err.is_not_found());
    assert_eq!(err.to_string(), "not found: doc");
}

#[test]
fn forward_direct_timeout_preserves_variant() {
    let result: Result<String, TargetErrors> = source_timeout().forward("forwarding directly");

    let err = result.unwrap_err();
    assert!(err.is_timeout());
    assert_eq!(err.to_string(), "timeout after 3000ms");
}

#[test]
fn forward_success_passes_through() {
    let result: Result<String, TargetErrors> = source_success().forward("forwarding directly");

    assert_eq!(result.unwrap(), "hello");
}

// ---------------------------------------------------------------------------
// Acceptance test #17: Lazy context variants
// ---------------------------------------------------------------------------

#[test]
fn forward_with_closure_not_called_on_success() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    CALLED.store(false, Ordering::SeqCst);
    let result: Result<String, TargetErrors> = source_success().forward_with(|| {
        CALLED.store(true, Ordering::SeqCst);
        "should not be called".to_string()
    });

    assert_eq!(result.unwrap(), "hello");
    assert!(
        !CALLED.load(Ordering::SeqCst),
        "closure should NOT be called on success path"
    );
}

#[test]
fn forward_with_closure_called_on_error_path() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    CALLED.store(false, Ordering::SeqCst);
    let result: Result<String, TargetErrors> = source_not_found().forward_with(|| {
        CALLED.store(true, Ordering::SeqCst);
        "direct mapping context".to_string()
    });

    let err = result.unwrap_err();
    assert!(err.is_not_found());
    assert!(
        CALLED.load(Ordering::SeqCst),
        "closure SHOULD be called on the error path"
    );
}

// ---------------------------------------------------------------------------
// Matched::forward_with lazy context
// ---------------------------------------------------------------------------

#[test]
fn matched_forward_with_lazy_not_called_on_ok() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn inner() -> Result<String, TargetErrors> {
        CALLED.store(false, Ordering::SeqCst);
        // When try_match returns Ok, forward_with on Matched is never reached.
        let value = source_success().try_match("step a")?.map_err(|m| {
            CALLED.store(true, Ordering::SeqCst);
            m.forward_with::<TargetErrors, _>(|| "should not be called".to_string())
        })?;
        Ok(value)
    }

    let result = inner();
    assert_eq!(result.unwrap(), "hello");
    assert!(
        !CALLED.load(Ordering::SeqCst),
        "closure should NOT be called when try_match returns Ok"
    );
}

#[test]
fn matched_forward_with_lazy_called_on_error_path() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn inner() -> Result<String, TargetErrors> {
        CALLED.store(false, Ordering::SeqCst);
        let value = source_not_found().try_match("step a")?.map_err(|m| {
            CALLED.store(true, Ordering::SeqCst);
            m.forward_with::<TargetErrors, _>(|| "direct mapping context".to_string())
        })?;
        Ok(value)
    }

    let result = inner();
    let err = result.unwrap_err();
    assert!(err.is_not_found());
    assert!(
        CALLED.load(Ordering::SeqCst),
        "closure SHOULD be called on the error path"
    );
}
