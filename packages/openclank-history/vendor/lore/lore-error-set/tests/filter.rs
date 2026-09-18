// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for `try_match` filtering behavior.
//!
//! Covers spec acceptance tests:
//! - #12: `try_match("context")?` — Internal propagates with context,
//!   handleable errors return Matched
//! - #13: Matched wrapper exhaustiveness — compiler requires all handleable
//!   arms (no Internal variant, no Ok variant)

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;
use lore_error_set::Internal;
use lore_error_set::ResultExt;
use lore_error_set::Traced;

// ---------------------------------------------------------------------------
// Discrete error types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
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

// ---------------------------------------------------------------------------
// Error set
// ---------------------------------------------------------------------------

#[error_set]
pub enum FilterErrors {
    NotFound,
    Timeout,
}

// ---------------------------------------------------------------------------
// Acceptance test #12: try_match propagates Internal, returns Matched for
// handleable errors
// ---------------------------------------------------------------------------

/// Helper that returns a success result.
fn success_op() -> Result<u32, FilterErrors> {
    Ok(42)
}

/// Helper that returns a `NotFound` error.
fn not_found_op() -> Result<u32, FilterErrors> {
    Err(NotFound {
        resource: "item".into(),
    }
    .into())
}

/// Helper that returns a Timeout error.
fn timeout_op() -> Result<u32, FilterErrors> {
    Err(Timeout { duration_ms: 500 }.into())
}

/// Helper that returns an Internal error.
fn internal_op() -> Result<u32, FilterErrors> {
    let io_err = std::io::Error::other("disk failure");
    Err(FilterErrors::internal_with_context(
        io_err,
        "original context",
    ))
}

#[test]
fn try_match_success_returns_ok() {
    fn inner() -> Result<u32, Traced<Internal>> {
        let matched = success_op().try_match("filtering")?;
        match matched {
            Ok(v) => Ok(v),
            Err(MatchedFilterErrors::NotFound(_)) => panic!("unexpected NotFound"),
            Err(MatchedFilterErrors::Timeout(_)) => panic!("unexpected Timeout"),
        }
    }

    let result = inner();
    assert_eq!(result.unwrap(), 42);
}

#[test]
fn try_match_handleable_error_returns_matched_variant() {
    fn inner() -> Result<u32, Traced<Internal>> {
        let matched = not_found_op().try_match("filtering")?;
        match matched {
            Ok(v) => Ok(v),
            Err(MatchedFilterErrors::NotFound(e)) => {
                // Verify we can access the inner error.
                assert_eq!(e.resource, "item");
                Ok(999)
            }
            Err(MatchedFilterErrors::Timeout(_)) => panic!("unexpected Timeout"),
        }
    }

    let result = inner();
    assert_eq!(result.unwrap(), 999);
}

#[test]
fn try_match_internal_propagates_with_context() {
    fn inner() -> Result<u32, Traced<Internal>> {
        let matched = internal_op().try_match("filtering step")?;
        match matched {
            Ok(v) => Ok(v),
            Err(MatchedFilterErrors::NotFound(_)) => panic!("unexpected NotFound"),
            Err(MatchedFilterErrors::Timeout(_)) => panic!("unexpected Timeout"),
        }
    }

    let result = inner();
    assert!(result.is_err());
    let traced = result.unwrap_err();
    // Trace contains both the original-construction context and the hop.
    let locations = traced.trace().locations();
    assert_eq!(
        locations.first().and_then(|l| l.context()),
        Some("original context")
    );
    assert_eq!(
        locations.last().and_then(|l| l.context()),
        Some("filtering step")
    );
    // Source skips directly to the io::Error — no nested Internal.
    let source = traced.source().expect("should have source");
    assert!(
        source.downcast_ref::<std::io::Error>().is_some(),
        "source should be io::Error, not a nested Internal"
    );
    // The enum-level Display walks the trace; the bare Traced<Internal>
    // does not (the generic Traced<E>: Display impl just delegates).
    let enum_err: FilterErrors = traced.into();
    assert_eq!(enum_err.to_string(), "filtering step: disk failure");
}

// ---------------------------------------------------------------------------
// Acceptance test #13: Matched wrapper exhaustiveness — all handleable
// variants required, no Internal, no Ok
// ---------------------------------------------------------------------------

#[test]
fn matched_is_exhaustive_over_handleable_variants() {
    // This test verifies that the match is exhaustive without an Internal arm.
    // If the Matched enum had an Internal variant, this would fail to compile
    // without it.
    let result = timeout_op();
    let matched = result.try_match("exhaustive test");

    // The try_match itself returns Result<Result<T, Matched>, Internal>.
    // On error path, Internal is separated out.
    let matched = matched.expect("should not be Internal");
    let msg = match matched {
        Ok(v) => format!("ok: {v}"),
        Err(MatchedFilterErrors::NotFound(e)) => format!("not found: {}", e.resource),
        Err(MatchedFilterErrors::Timeout(e)) => format!("timeout: {}ms", e.duration_ms),
        // No Internal arm needed — it was propagated by try_match
    };
    assert_eq!(msg, "timeout: 500ms");
}

// ---------------------------------------------------------------------------
// try_match_with: lazy context only called on Internal path
// ---------------------------------------------------------------------------

#[test]
fn try_match_with_lazy_context_not_called_on_success() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn inner() -> Result<u32, Traced<Internal>> {
        CALLED.store(false, Ordering::SeqCst);
        let matched = success_op().try_match_with(|| {
            CALLED.store(true, Ordering::SeqCst);
            "should not be called".to_string()
        })?;
        Ok(matched.expect("unexpected error"))
    }

    let result = inner();
    assert_eq!(result.unwrap(), 42);
    assert!(
        !CALLED.load(Ordering::SeqCst),
        "closure should NOT be called on success"
    );
}

#[test]
fn try_match_with_lazy_context_called_on_handleable_error() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn inner() -> Result<u32, Traced<Internal>> {
        CALLED.store(false, Ordering::SeqCst);
        let matched = not_found_op().try_match_with(|| {
            CALLED.store(true, Ordering::SeqCst);
            "handleable context".to_string()
        })?;
        match matched {
            Ok(v) => Ok(v),
            Err(MatchedFilterErrors::NotFound(_)) => Ok(0),
            Err(MatchedFilterErrors::Timeout(_)) => panic!("unexpected"),
        }
    }

    let result = inner();
    assert_eq!(result.unwrap(), 0);
    assert!(
        CALLED.load(Ordering::SeqCst),
        "closure SHOULD be called on all error paths"
    );
}

#[test]
fn try_match_with_lazy_context_called_on_internal() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn inner() -> Result<u32, Traced<Internal>> {
        CALLED.store(false, Ordering::SeqCst);
        let matched = internal_op().try_match_with(|| {
            CALLED.store(true, Ordering::SeqCst);
            "lazy context evaluated".to_string()
        })?;
        Ok(matched.expect("unexpected error"))
    }

    let result = inner();
    assert!(result.is_err());
    assert!(
        CALLED.load(Ordering::SeqCst),
        "closure SHOULD be called for Internal errors"
    );
    let traced = result.unwrap_err();
    let locations = traced.trace().locations();
    assert_eq!(
        locations.first().and_then(|l| l.context()),
        Some("original context")
    );
    assert_eq!(
        locations.last().and_then(|l| l.context()),
        Some("lazy context evaluated")
    );
    // Enum-level Display walks the trace.
    let enum_err: FilterErrors = traced.into();
    assert_eq!(enum_err.to_string(), "lazy context evaluated: disk failure");
}
