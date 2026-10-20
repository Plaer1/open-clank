// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for the `Matched` enum and catch-all forward pattern.
//!
//! Covers spec acceptance tests:
//! - #19: Catch-all forward — handle one variant, forward the rest via
//!   `Err(other) => return Err(other.forward("ctx"))`

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
// Target error set (has NotFound only)
// ---------------------------------------------------------------------------

#[error_set]
pub enum TargetErrors {
    NotFound,
    Timeout,
    RateLimit,
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

fn source_rate_limit() -> Result<String, SourceErrors> {
    Err(RateLimit.into())
}

// ---------------------------------------------------------------------------
// Acceptance test #19: Catch-all forward
// Handle NotFound explicitly, forward everything else
// ---------------------------------------------------------------------------

#[test]
fn catch_all_forward_handle_one_forward_rest() {
    fn caller() -> Result<String, TargetErrors> {
        let value = match source_not_found().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "default_value".to_string(),
            Err(other) => return Err(other.forward("unhandled error")),
        };
        Ok(value)
    }

    let result = caller();
    assert_eq!(result.unwrap(), "default_value");
}

#[test]
fn catch_all_forward_success_returns_value() {
    fn caller() -> Result<String, TargetErrors> {
        let value = match source_success().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "default_value".to_string(),
            Err(other) => return Err(other.forward("unhandled error")),
        };
        Ok(value)
    }

    let result = caller();
    assert_eq!(result.unwrap(), "value");
}

#[test]
fn catch_all_forward_unhandled_maps_to_target() {
    fn caller() -> Result<String, TargetErrors> {
        let value = match source_timeout().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "default_value".to_string(),
            Err(other) => return Err(other.forward("unhandled error")),
        };
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    // TargetErrors declares Timeout, so the catch-all maps directly.
    assert!(err.is_timeout());
    assert_eq!(err.to_string(), "timeout after 2000ms");
}

#[test]
fn catch_all_forward_another_unhandled_variant() {
    fn caller() -> Result<String, TargetErrors> {
        let value = match source_rate_limit().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "default_value".to_string(),
            Err(other) => return Err(other.forward("unhandled error")),
        };
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    // TargetErrors declares RateLimit, so it maps directly.
    assert!(err.is_rate_limit());
    assert_eq!(err.to_string(), "rate limited");
}

// ---------------------------------------------------------------------------
// Catch-all with forward_with (lazy context)
// ---------------------------------------------------------------------------

#[test]
fn catch_all_forward_with_lazy_context() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    fn caller() -> Result<String, TargetErrors> {
        CALLED.store(false, Ordering::SeqCst);
        let value = match source_timeout().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "default_value".to_string(),
            Err(other) => {
                return Err(other.forward_with(|| {
                    CALLED.store(true, Ordering::SeqCst);
                    "lazy unhandled context".to_string()
                }))
            }
        };
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    assert!(err.is_timeout());
    assert!(CALLED.load(Ordering::SeqCst));
}

// ---------------------------------------------------------------------------
// Matched enum has Debug impl
// ---------------------------------------------------------------------------

#[test]
fn matched_enum_has_debug_impl() {
    let matched = MatchedSourceErrors::NotFound(lore_error_set::Traced::new(
        NotFound {
            resource: "test".into(),
        },
        lore_error_set::Trace::new(),
    ));
    let debug_str = format!("{matched:?}");
    assert!(debug_str.contains("NotFound"));
}

// ---------------------------------------------------------------------------
// Multiple explicit handles with catch-all
// ---------------------------------------------------------------------------

#[test]
fn multiple_explicit_handles_with_catch_all() {
    /// A target set covering every source variant — required for strict forward.
    #[error_set]
    pub enum BroadTarget {
        NotFound,
        Timeout,
        RateLimit,
    }

    fn caller() -> Result<String, BroadTarget> {
        let value = match source_rate_limit().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "not found fallback".to_string(),
            Err(MatchedSourceErrors::Timeout(_e)) => "timeout fallback".to_string(),
            Err(other @ MatchedSourceErrors::RateLimit(_)) => {
                return Err(other.forward("unhandled"))
            }
        };
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    // RateLimit maps directly because BroadTarget declares it.
    assert!(err.is_rate_limit());
}

// ---------------------------------------------------------------------------
// Matched forward when error type exists in target set
// ---------------------------------------------------------------------------

#[test]
fn matched_forward_maps_directly_when_type_exists() {
    /// Target that covers every source variant.
    #[error_set]
    pub enum WideTarget {
        NotFound,
        Timeout,
        RateLimit,
    }

    fn caller() -> Result<String, WideTarget> {
        let value = match source_timeout().try_match("loading")? {
            Ok(v) => v,
            Err(MatchedSourceErrors::NotFound(_e)) => "handled".to_string(),
            Err(other) => return Err(other.forward("forwarding")),
        };
        Ok(value)
    }

    let result = caller();
    let err = result.unwrap_err();
    // Timeout exists in WideTarget, so it should map directly.
    assert!(err.is_timeout());
}
