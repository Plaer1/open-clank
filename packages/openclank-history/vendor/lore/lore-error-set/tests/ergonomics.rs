// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for error set ergonomics — accessor methods.
//!
//! Covers spec acceptance tests:
//! - #8: `is_not_found()` returns correct bool
//! - #9: `as_not_found()` returns `Some(&NotFound)` / `None`
//! - Accessor methods use correct `snake_case` naming for multi-word types
//! - `as_*_traced()` returns `Option<&Traced<T>>`

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;

// ---------------------------------------------------------------------------
// Discrete error types
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq)]
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

#[derive(Debug, PartialEq)]
pub struct AlreadyExists {
    name: String,
}

impl fmt::Display for AlreadyExists {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "already exists: {}", self.name)
    }
}

impl Error for AlreadyExists {}

impl FfiError for AlreadyExists {
    fn ffi_code(&self) -> i32 {
        11
    }
}

#[derive(Debug, PartialEq)]
pub struct Timeout;

impl fmt::Display for Timeout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "timeout")
    }
}

impl Error for Timeout {}

impl FfiError for Timeout {
    fn ffi_code(&self) -> i32 {
        12
    }
}

// ---------------------------------------------------------------------------
// Error set
// ---------------------------------------------------------------------------

#[error_set]
pub enum ApiError {
    NotFound,
    AlreadyExists,
    Timeout,
}

// ---------------------------------------------------------------------------
// Acceptance test #8: is_not_found() returns correct bool
// ---------------------------------------------------------------------------

#[test]
fn is_not_found_returns_true_for_not_found() {
    let err: ApiError = NotFound {
        resource: "user".into(),
    }
    .into();
    assert!(err.is_not_found());
    assert!(!err.is_already_exists());
    assert!(!err.is_timeout());
    assert!(!err.is_internal());
}

#[test]
fn is_already_exists_returns_true_for_already_exists() {
    let err: ApiError = AlreadyExists { name: "foo".into() }.into();
    assert!(!err.is_not_found());
    assert!(err.is_already_exists());
    assert!(!err.is_timeout());
    assert!(!err.is_internal());
}

#[test]
fn is_timeout_returns_true_for_timeout() {
    let err: ApiError = Timeout.into();
    assert!(!err.is_not_found());
    assert!(!err.is_already_exists());
    assert!(err.is_timeout());
    assert!(!err.is_internal());
}

#[test]
fn is_internal_returns_true_for_internal() {
    use lore_error_set::ErrorSet;
    let io_err = std::io::Error::other("oops");
    let traced_box = lore_error_set::TracedBox::new(Box::new(io_err), lore_error_set::Trace::new());
    let err = ApiError::wrap_internal(traced_box, "test");
    assert!(!err.is_not_found());
    assert!(!err.is_already_exists());
    assert!(!err.is_timeout());
    assert!(err.is_internal());
}

// ---------------------------------------------------------------------------
// Acceptance test #9: as_not_found() returns Some(&NotFound) / None
// ---------------------------------------------------------------------------

#[test]
fn as_not_found_returns_some_for_not_found() {
    let err: ApiError = NotFound {
        resource: "item".into(),
    }
    .into();

    let inner = err.as_not_found().expect("should return Some");
    assert_eq!(inner.resource, "item");
}

#[test]
fn as_not_found_returns_none_for_other_variant() {
    let err: ApiError = Timeout.into();
    assert!(err.as_not_found().is_none());
}

#[test]
fn as_already_exists_returns_some_for_already_exists() {
    let err: ApiError = AlreadyExists { name: "bar".into() }.into();

    let inner = err.as_already_exists().expect("should return Some");
    assert_eq!(inner.name, "bar");
}

#[test]
fn as_internal_returns_some_for_internal() {
    let io_err = std::io::Error::other("boom");
    let err = ApiError::internal_with_context(io_err, "ctx");
    let internal = err.as_internal().expect("should return Some");
    // Internal is now a pure data type — Display delegates to source.
    assert_eq!(internal.to_string(), "boom");
}

#[test]
fn as_internal_returns_none_for_user_variant() {
    let err: ApiError = Timeout.into();
    assert!(err.as_internal().is_none());
}

// ---------------------------------------------------------------------------
// as_*_traced() returns Traced reference
// ---------------------------------------------------------------------------

#[test]
fn as_not_found_traced_returns_traced_ref() {
    let err: ApiError = NotFound {
        resource: "r".into(),
    }
    .into();

    let traced = err.as_not_found_traced().expect("should return Some");
    // Can access the inner via Deref.
    assert_eq!(traced.resource, "r");
    // Can access the trace.
    let _trace = traced.trace();
}

#[test]
fn as_timeout_traced_returns_none_for_other() {
    let err: ApiError = NotFound {
        resource: "x".into(),
    }
    .into();
    assert!(err.as_timeout_traced().is_none());
}

// ---------------------------------------------------------------------------
// Snake_case naming for multi-word variants
// ---------------------------------------------------------------------------

#[test]
fn snake_case_methods_for_multi_word_type() {
    // AlreadyExists -> is_already_exists, as_already_exists, as_already_exists_traced
    let err: ApiError = AlreadyExists { name: "dup".into() }.into();

    assert!(err.is_already_exists());
    assert!(err.as_already_exists().is_some());
    assert!(err.as_already_exists_traced().is_some());
}
