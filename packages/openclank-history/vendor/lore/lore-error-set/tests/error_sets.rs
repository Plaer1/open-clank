// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for error set composition.
//!
//! Covers spec acceptance tests:
//! - #2: Compose error set with 3 variants -> enum has 4 (+ Internal)
//! - #7: Pattern matching compiles
//! - #10: thiserror-derived error type works in error set
//! - #11: Manually-implemented Error type works in error set

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;

// ---------------------------------------------------------------------------
// Discrete error types (manually implemented)
// ---------------------------------------------------------------------------

/// A manual error type for "not found" errors.
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

/// A manual error type for "timeout" errors.
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

/// A manual error type for "permission denied" errors.
#[derive(Debug)]
pub struct PermissionDenied;

impl fmt::Display for PermissionDenied {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "permission denied")
    }
}

impl Error for PermissionDenied {}

impl FfiError for PermissionDenied {
    fn ffi_code(&self) -> i32 {
        12
    }
}

// ---------------------------------------------------------------------------
// Error set with 3 variants
// ---------------------------------------------------------------------------

#[error_set]
pub enum ServiceError {
    NotFound,
    Timeout,
    PermissionDenied,
}

// ---------------------------------------------------------------------------
// Acceptance test #2: Compose error set with 3 variants -> enum has 4
// ---------------------------------------------------------------------------

#[test]
fn error_set_has_user_variants_plus_internal() {
    // Verify each variant can be constructed.
    let nf: ServiceError = NotFound {
        resource: "file.txt".into(),
    }
    .into();
    assert!(nf.is_not_found());

    let to: ServiceError = Timeout { duration_ms: 5000 }.into();
    assert!(to.is_timeout());

    let pd: ServiceError = PermissionDenied.into();
    assert!(pd.is_permission_denied());

    // Internal variant exists and is constructible through wrap_internal.
    use lore_error_set::ErrorSet;
    let internal_source = std::io::Error::other("unexpected");
    let traced_box =
        lore_error_set::TracedBox::new(Box::new(internal_source), lore_error_set::Trace::new());
    let internal = ServiceError::wrap_internal(traced_box, "test context");
    assert!(internal.is_internal());
}

// ---------------------------------------------------------------------------
// Acceptance test #7: Pattern matching compiles
// ---------------------------------------------------------------------------

#[test]
fn pattern_matching_compiles() {
    let err: ServiceError = NotFound {
        resource: "db".into(),
    }
    .into();

    let msg = match err {
        ServiceError::NotFound(e) => format!("got not found: {e}"),
        ServiceError::Timeout(e) => format!("got timeout: {e}"),
        ServiceError::PermissionDenied(e) => format!("got permission denied: {e}"),
        ServiceError::Internal(e) => format!("got internal: {e}"),
    };

    assert!(msg.starts_with("got not found:"));
}

// ---------------------------------------------------------------------------
// Acceptance test #10: thiserror-derived error type works in error set
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
#[error("thiserror not found: {resource}")]
pub struct ThiserrorNotFound {
    resource: String,
}

#[derive(Debug, thiserror::Error)]
#[error("thiserror timeout")]
pub struct ThiserrorTimeout;

impl FfiError for ThiserrorNotFound {
    fn ffi_code(&self) -> i32 {
        20
    }
}

impl FfiError for ThiserrorTimeout {
    fn ffi_code(&self) -> i32 {
        21
    }
}

#[error_set]
pub enum ThiserrorSet {
    ThiserrorNotFound,
    ThiserrorTimeout,
}

#[test]
fn thiserror_derived_type_works_in_error_set() {
    let err: ThiserrorSet = ThiserrorNotFound {
        resource: "table".into(),
    }
    .into();

    assert!(err.is_thiserror_not_found());
    assert_eq!(err.to_string(), "thiserror not found: table");
}

// ---------------------------------------------------------------------------
// Acceptance test #11: Manually-implemented Error type works in error set
// ---------------------------------------------------------------------------

#[test]
fn manually_implemented_error_type_works() {
    let err: ServiceError = Timeout { duration_ms: 100 }.into();
    assert!(err.is_timeout());
    assert_eq!(err.to_string(), "timeout after 100ms");
}

// ---------------------------------------------------------------------------
// Debug impl works
// ---------------------------------------------------------------------------

#[test]
fn debug_impl_works() {
    let err: ServiceError = NotFound {
        resource: "x".into(),
    }
    .into();
    let debug_str = format!("{err:?}");
    assert!(debug_str.contains("NotFound"));
}

// ---------------------------------------------------------------------------
// Display delegates correctly (no duplication)
// ---------------------------------------------------------------------------

#[test]
fn display_delegates_to_inner() {
    let err: ServiceError = NotFound {
        resource: "item".into(),
    }
    .into();
    assert_eq!(err.to_string(), "not found: item");

    let err: ServiceError = Timeout { duration_ms: 42 }.into();
    assert_eq!(err.to_string(), "timeout after 42ms");
}

// ---------------------------------------------------------------------------
// Error::source delegates through Traced<E> via Deref
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct InnerCause;

impl fmt::Display for InnerCause {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "inner cause")
    }
}

impl Error for InnerCause {}

/// An error type that has a source.
#[derive(Debug)]
pub struct WithSource {
    source: InnerCause,
}

impl fmt::Display for WithSource {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "with source")
    }
}

impl Error for WithSource {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.source)
    }
}

impl FfiError for WithSource {
    fn ffi_code(&self) -> i32 {
        30
    }
}

#[error_set]
pub enum SourceTestSet {
    WithSource,
}

#[test]
fn error_source_delegates_through_deref() {
    let err: SourceTestSet = WithSource { source: InnerCause }.into();

    let source = err.source().expect("should have source");
    assert_eq!(source.to_string(), "inner cause");
}

// ---------------------------------------------------------------------------
// From impl allows ? operator
// ---------------------------------------------------------------------------

fn fallible_not_found() -> Result<(), NotFound> {
    Err(NotFound {
        resource: "missing".into(),
    })
}

fn uses_question_mark() -> Result<(), ServiceError> {
    fallible_not_found()?;
    Ok(())
}

#[test]
fn from_impl_allows_question_mark() {
    let result = uses_question_mark();
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(err.is_not_found());
}
