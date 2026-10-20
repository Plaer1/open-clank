// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for cross-set error mapping.
//!
//! Covers spec acceptance tests:
//! - #3: Direct mapping — same type in both sets maps directly
//! - #4: Internal mapping — type not in target set becomes Internal with context
//! - #5: Source preservation — `.source()` returns original error after Internal wrap
//!
//! Note: These tests exercise the `ErrorSet` trait methods directly
//! (`extract_inner`, `try_from_inner`, `wrap_internal`) since `ResultExt::forward()`
//! is not implemented until TASK-003.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::ErrorSet;
use lore_error_set::FfiError;
use lore_error_set::ForwardStrict;

// ---------------------------------------------------------------------------
// Shared discrete error types
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

#[error_set]
pub enum TargetErrors {
    NotFound,
    Timeout,
    RateLimit,
}

// ---------------------------------------------------------------------------
// Acceptance test #3: Direct mapping — NotFound in both sets maps directly
// ---------------------------------------------------------------------------

#[test]
fn direct_mapping_same_type_in_both_sets() {
    let source_err: SourceErrors = NotFound {
        resource: "user:42".into(),
    }
    .into();

    // Extract from source, try to fit into target.
    let traced_box = source_err.extract_inner();
    let result = TargetErrors::try_from_inner(traced_box);

    // Should succeed because NotFound exists in TargetErrors.
    let target_err = result.expect("should match NotFound");
    assert!(target_err.is_not_found());
    assert_eq!(target_err.to_string(), "not found: user:42");

    // Verify the inner type is preserved.
    let inner = target_err.as_not_found().expect("should be NotFound");
    assert_eq!(inner.resource, "user:42");
}

// ---------------------------------------------------------------------------
// Acceptance test #4: Direct variant mapping under strict forward.
// ---------------------------------------------------------------------------

#[test]
fn direct_mapping_preserves_variant() {
    let result: Result<(), TargetErrors> =
        Err::<(), SourceErrors>(Timeout { duration_ms: 5000 }.into())
            .forward("forwarding from SourceErrors");
    let target_err = result.unwrap_err();

    assert!(target_err.is_timeout());
    assert_eq!(target_err.to_string(), "timeout after 5000ms");
}

// ---------------------------------------------------------------------------
// Acceptance test #5: Source preservation — .source() returns original error
// after Internal wrap
// ---------------------------------------------------------------------------

#[test]
fn source_preservation_after_internal_wrap() {
    let io_err = std::io::Error::other("timeout after 1234ms");
    let traced_box = lore_error_set::TracedBox::new(
        Box::new(io_err) as Box<dyn std::error::Error + Send + Sync + 'static>,
        lore_error_set::Trace::new(),
    );
    let target_err = TargetErrors::wrap_internal(traced_box, "mapping context");

    // The Internal variant should have a source.
    let source = target_err.source().expect("Internal should have a source");

    // The source should display the original error message.
    assert_eq!(source.to_string(), "timeout after 1234ms");
}

// ---------------------------------------------------------------------------
// Full forward simulation: extract + try_from_inner + wrap_internal
// ---------------------------------------------------------------------------

fn simulate_forward<Source, Target>(source: Source, context: &str) -> Target
where
    Source: ErrorSet,
    Target: ErrorSet + lore_error_set::HasAll<<Source as ErrorSet>::Variants>,
{
    // Use ForwardStrict::forward so the hop context lands on the trace via the
    // same push that real forward callers go through.
    Err::<(), Source>(source)
        .forward::<Target>(context)
        .unwrap_err()
}

#[test]
fn full_forward_direct_match() {
    let source_err: SourceErrors = NotFound {
        resource: "doc".into(),
    }
    .into();

    let target_err: TargetErrors = simulate_forward(source_err, "forwarding");

    assert!(target_err.is_not_found());
    assert_eq!(target_err.to_string(), "not found: doc");
}

#[test]
fn full_forward_rate_limit_direct_match() {
    let source_err: SourceErrors = RateLimit.into();

    let target_err: TargetErrors = simulate_forward(source_err, "forwarding rate limit");

    assert!(target_err.is_rate_limit());
    assert_eq!(target_err.to_string(), "rate limited");
}

// ---------------------------------------------------------------------------
// Internal variant from source set maps to Internal in target set
// ---------------------------------------------------------------------------

#[test]
fn internal_in_source_maps_to_internal_in_target() {
    // Create an Internal variant in the source set, seeding the trace with
    // a context-bearing entry.
    let io_err = std::io::Error::other("disk failure");
    let source_err = SourceErrors::internal_with_context(io_err, "source context");

    // Forward to target set.
    let target_err: TargetErrors = simulate_forward(source_err, "target context");

    assert!(target_err.is_internal());
    // Flattened: target Internal IS the source Internal (adopted). Source
    // is io::Error directly, no nested Internal. The construction context
    // is preserved on the trace.
    let source = target_err.source().expect("should have source");
    assert!(
        source.downcast_ref::<std::io::Error>().is_some(),
        "source should be io::Error directly, not a nested Internal"
    );
    assert!(
        target_err
            .trace()
            .locations()
            .iter()
            .any(|l| l.context() == Some("source context")),
        "construction context should appear on trace"
    );
}
