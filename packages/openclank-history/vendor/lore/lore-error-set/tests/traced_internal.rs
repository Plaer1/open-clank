// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Regression tests for the `Internal(Traced<Internal>)` refactor.
//!
//! Covers the two behaviours the refactor was meant to fix:
//! 1. Cross-set hops into Internal produce a single canonical trace surface
//!    on the outer `Traced<>`, with no duplicate trace storage on a nested
//!    boxed Internal.
//! 2. `From<Internal> for #Enum` is `#[track_caller]` and records the
//!    `.into()` / `?` site as a Location on the resulting trace.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::FfiError;
use lore_error_set::ForwardStrict;
use lore_error_set::Internal;
use lore_error_set::Traced;

// ---------------------------------------------------------------------------
// Discrete error types — Source has Boom, Target has only Bar (no Boom).
// Forwarding a Source::Boom into Target therefore takes the mismatch path
// and lands in Target::Internal.
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct Boom;
impl fmt::Display for Boom {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "boom")
    }
}
impl Error for Boom {}
impl FfiError for Boom {
    fn ffi_code(&self) -> i32 {
        100
    }
}

#[derive(Debug)]
pub struct Bar;
impl fmt::Display for Bar {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "bar")
    }
}
impl Error for Bar {}
impl FfiError for Bar {
    fn ffi_code(&self) -> i32 {
        101
    }
}

#[error_set]
pub enum SourceErr {
    Boom,
}

#[error_set]
pub enum TargetErr {
    Bar,
    Boom,
}

// ---------------------------------------------------------------------------
// 1. Forward into a mismatching target — single canonical trace surface.
// ---------------------------------------------------------------------------

#[test]
#[cfg(feature = "track-locations")]
fn forward_into_internal_flattens_no_nesting() {
    // Build an upstream Source::Internal — internal_with_context seeds the
    // trace with one Location::with_context("upstream context") entry at
    // the current call site.
    let io_err = std::io::Error::other("disk failure");
    let upstream: SourceErr = SourceErr::internal_with_context(io_err, "upstream context");
    assert_eq!(upstream.trace().len(), 1);

    // .forward() into TargetErr — SourceErr's Internal variant always takes
    // the mismatch path. wrap_internal must NOT wrap the upstream Internal
    // in a new Internal; it must adopt the upstream and append a hop entry
    // to the trace.
    let downstream: TargetErr = Err::<(), _>(upstream)
        .forward("forwarded into target")
        .unwrap_err();
    assert!(downstream.is_internal());

    // Outer trace = upstream(1) + forward hop(1).
    let outer_trace = downstream.trace();
    assert_eq!(outer_trace.len(), 2);
    assert_eq!(
        outer_trace.locations()[0].context(),
        Some("upstream context"),
        "upstream construction context should appear on trace"
    );
    let hop = &outer_trace.locations()[1];
    assert!(hop.file.ends_with("traced_internal.rs"));
    assert_eq!(hop.context(), Some("forwarded into target"));

    // Enum-level Display walks the trace newest-first and prepends the
    // most-recent context to the source's Display.
    assert_eq!(
        downstream.to_string(),
        "forwarded into target: disk failure"
    );

    // The source chain skips directly to the io::Error — no nested Internal
    // sits between the outer Internal and the io::Error.
    let source = downstream.source().expect("Internal should have a source");
    let io_err = source
        .downcast_ref::<std::io::Error>()
        .expect("source should be io::Error, not a nested Internal");
    assert_eq!(io_err.to_string(), "disk failure");
}

// ---------------------------------------------------------------------------
// 2. `From<Internal> for #Enum` records the `.into()` site.
// ---------------------------------------------------------------------------

#[test]
#[cfg(feature = "track-locations")]
fn from_internal_records_into_site() {
    let internal = Internal::msg("something went wrong");
    let err: SourceErr = internal.into();

    assert!(err.is_internal());
    let trace = err.trace();
    assert_eq!(
        trace.len(),
        1,
        "From<Internal> should push exactly one Location"
    );
    let loc = &trace.locations()[0];
    assert!(
        loc.file.ends_with("traced_internal.rs"),
        "location should point at this file; got {}",
        loc.file
    );
}

#[test]
#[cfg(feature = "track-locations")]
fn from_traced_internal_does_not_re_trace() {
    // A Traced<Internal> already carries a trace; `From<Traced<Internal>> for #Enum`
    // must wrap without pushing a new entry.
    let mut trace = lore_error_set::Trace::new();
    trace.push(lore_error_set::Location::new("explicit.rs", 42, 1));
    let traced: Traced<Internal> = Traced::new(Internal::msg("explicit"), trace);

    let err: SourceErr = traced.into();
    assert!(err.is_internal());
    let final_trace = err.trace();
    assert_eq!(
        final_trace.len(),
        1,
        "From<Traced<Internal>> must not push a new entry"
    );
    assert_eq!(final_trace.locations()[0].file, "explicit.rs");
    assert_eq!(final_trace.locations()[0].line, 42);
}
