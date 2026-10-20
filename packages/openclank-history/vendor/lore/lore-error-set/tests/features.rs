// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
#![cfg(not(feature = "track-locations"))]
//! Integration tests verifying zero-cost tracing when track-locations is disabled.
//!
//! Covers spec acceptance test #14 (disabled): Feature-gated tracing disabled
//! means empty trace. `Traced<E>` is essentially zero-cost and trace methods
//! return empty results.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;

// ---------------------------------------------------------------------------
// Discrete error types
// ---------------------------------------------------------------------------

#[derive(Debug)]
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
pub struct Timeout;

impl fmt::Display for Timeout {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "timeout")
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
pub enum FeatureTestSet {
    NotFound,
    Timeout,
}

// ---------------------------------------------------------------------------
// Spec test #14 (disabled): Trace is empty when feature is disabled
// ---------------------------------------------------------------------------

#[test]
fn trace_is_empty_when_feature_disabled() {
    let err: FeatureTestSet = NotFound {
        resource: "item".into(),
    }
    .into();

    // Access trace via as_*_traced() -> trace should be empty.
    let traced = err
        .as_not_found_traced()
        .expect("as_not_found_traced should return Some");
    let trace = traced.trace();

    assert!(
        trace.is_empty(),
        "trace should be empty when track-locations is disabled"
    );
    assert_eq!(
        trace.len(),
        0,
        "trace len should be 0 when track-locations is disabled"
    );
    assert!(
        trace.locations().is_empty(),
        "trace locations should be empty when track-locations is disabled"
    );
}

#[test]
fn trace_len_is_zero_when_feature_disabled() {
    let err: FeatureTestSet = Timeout.into();

    let traced = err
        .as_timeout_traced()
        .expect("as_timeout_traced should return Some");
    let trace = traced.trace();

    assert_eq!(trace.len(), 0, "trace len should be 0");
    assert!(trace.is_empty(), "trace should be empty");
    assert!(!trace.has_overflow(), "trace should not have overflow");
}

#[test]
fn trace_display_is_empty_when_feature_disabled() {
    let err: FeatureTestSet = NotFound {
        resource: "x".into(),
    }
    .into();

    let traced = err.as_not_found_traced().expect("should return Some");
    let trace = traced.trace();
    let display = format!("{trace}");

    assert!(
        display.is_empty(),
        "trace display should be empty when track-locations is disabled, got: {display:?}"
    );
}

#[test]
fn trace_on_error_set_is_empty_when_feature_disabled() {
    let err: FeatureTestSet = NotFound {
        resource: "item".into(),
    }
    .into();

    // Call .trace() directly on the error-set enum value.
    let trace = err.trace();
    assert!(
        trace.is_empty(),
        "trace should be empty when track-locations is disabled"
    );
    assert_eq!(
        trace.len(),
        0,
        "trace len should be 0 when track-locations is disabled"
    );
}

#[test]
fn traced_is_zero_cost_wrapper() {
    // Verify that Traced<E> is essentially zero-cost: the inner value is
    // accessible and trace methods return empty results.
    let err: FeatureTestSet = NotFound {
        resource: "test".into(),
    }
    .into();

    // as_not_found returns the inner value through Deref on Traced.
    let inner = err.as_not_found().expect("should return Some");
    assert_eq!(inner.resource, "test");

    // as_not_found_traced returns a Traced that derefs to the inner.
    let traced = err.as_not_found_traced().expect("should return Some");
    assert_eq!(traced.resource, "test");
}
