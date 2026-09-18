// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for the `WrapInternal` extension trait.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::prelude::*;

// ---------------------------------------------------------------------------
// Discrete error types (reused across tests)
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
        1
    }
}

// ---------------------------------------------------------------------------
// Error set
// ---------------------------------------------------------------------------

#[error_set]
pub enum TestError {
    NotFound,
}

// ---------------------------------------------------------------------------
// A helper that returns a std::io::Error for testing
// ---------------------------------------------------------------------------

fn failing_io() -> Result<(), std::io::Error> {
    Err(std::io::Error::new(
        std::io::ErrorKind::PermissionDenied,
        "access denied",
    ))
}

fn succeeding_io() -> Result<u32, std::io::Error> {
    Ok(42)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[test]
fn wrap_internal_maps_error_to_internal() {
    let result: Result<(), Traced<Internal>> = failing_io().internal("reading config");
    let err = result.unwrap_err();

    // Context lives on the trace's Location, not on Internal.
    assert_eq!(
        err.trace().locations().last().and_then(|l| l.context()),
        Some("reading config")
    );
    assert_eq!(err.to_string(), "access denied");
}

#[test]
fn wrap_internal_ok_passes_through() {
    let result: Result<u32, Traced<Internal>> = succeeding_io().internal("should not matter");
    assert_eq!(result.unwrap(), 42);
}

#[test]
fn wrap_internal_with_question_mark_into_error_set() {
    fn inner() -> Result<(), TestError> {
        failing_io().internal("reading config")?;
        Ok(())
    }

    let err = inner().unwrap_err();
    assert!(err.is_internal());
    // Display walks the trace newest-first for the most-recent context.
    assert_eq!(err.to_string(), "reading config: access denied");
}

#[test]
fn wrap_internal_preserves_source_chain() {
    let result: Result<(), Traced<Internal>> = failing_io().internal("ctx");
    let err = result.unwrap_err();

    let source = err.source().expect("should have a source");
    let io_err = source
        .downcast_ref::<std::io::Error>()
        .expect("source should be io::Error");
    assert_eq!(io_err.kind(), std::io::ErrorKind::PermissionDenied);
}

#[test]
fn internal_with_maps_error_with_lazy_context() {
    let hash = "abc123";
    let result: Result<(), Traced<Internal>> =
        failing_io().internal_with(|| format!("fetching object {hash}"));
    let err = result.unwrap_err();

    assert_eq!(
        err.trace().locations().last().and_then(|l| l.context()),
        Some("fetching object abc123")
    );
    assert_eq!(err.to_string(), "access denied");

    let source = err.source().expect("should have a source");
    let io_err = source
        .downcast_ref::<std::io::Error>()
        .expect("source should be io::Error");
    assert_eq!(io_err.kind(), std::io::ErrorKind::PermissionDenied);
}

#[test]
fn internal_with_closure_not_called_on_ok() {
    use std::sync::atomic::AtomicBool;
    use std::sync::atomic::Ordering;

    static CALLED: AtomicBool = AtomicBool::new(false);

    let result: Result<u32, Traced<Internal>> = succeeding_io().internal_with(|| {
        CALLED.store(true, Ordering::SeqCst);
        "should not run".to_string()
    });

    assert_eq!(result.unwrap(), 42);
    assert!(
        !CALLED.load(Ordering::SeqCst),
        "closure should not be called on Ok path"
    );
}

#[test]
fn internal_with_question_mark_into_error_set() {
    fn inner() -> Result<(), TestError> {
        failing_io().internal_with(|| format!("loading object {}", "xyz"))?;
        Ok(())
    }

    let err = inner().unwrap_err();
    assert!(err.is_internal());
    assert_eq!(err.to_string(), "loading object xyz: access denied");
}
