// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Integration tests for interoperability — thiserror and manual Error impls.
//!
//! Additional coverage for spec acceptance tests #10 and #11 with more
//! complex scenarios.

use std::error::Error;
use std::fmt;

use lore_error_set::error_set;
use lore_error_set::ErrorSet;
use lore_error_set::FfiError;

// ---------------------------------------------------------------------------
// thiserror-derived errors
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
#[error("database connection failed: {reason}")]
pub struct DbConnectionError {
    reason: String,
}

#[derive(Debug, thiserror::Error)]
#[error("query syntax error at position {position}")]
pub struct QuerySyntaxError {
    position: usize,
}

impl FfiError for DbConnectionError {
    fn ffi_code(&self) -> i32 {
        10
    }
}

impl FfiError for QuerySyntaxError {
    fn ffi_code(&self) -> i32 {
        11
    }
}

// ---------------------------------------------------------------------------
// Manually-implemented error
// ---------------------------------------------------------------------------

#[derive(Debug)]
pub struct SerializationError {
    message: String,
}

impl fmt::Display for SerializationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "serialization error: {}", self.message)
    }
}

impl Error for SerializationError {}

impl FfiError for SerializationError {
    fn ffi_code(&self) -> i32 {
        12
    }
}

// ---------------------------------------------------------------------------
// Mixed error set — thiserror + manual
// ---------------------------------------------------------------------------

#[error_set]
pub enum DataError {
    DbConnectionError,
    QuerySyntaxError,
    SerializationError,
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[test]
fn thiserror_display_in_error_set() {
    let err: DataError = DbConnectionError {
        reason: "refused".into(),
    }
    .into();
    assert_eq!(err.to_string(), "database connection failed: refused");
}

#[test]
fn thiserror_debug_in_error_set() {
    let err: DataError = QuerySyntaxError { position: 42 }.into();
    let debug_str = format!("{err:?}");
    assert!(debug_str.contains("QuerySyntaxError"));
}

#[test]
fn manual_error_display_in_error_set() {
    let err: DataError = SerializationError {
        message: "invalid utf-8".into(),
    }
    .into();
    assert_eq!(err.to_string(), "serialization error: invalid utf-8");
}

#[test]
fn mixed_error_set_accessor_methods() {
    let err: DataError = DbConnectionError {
        reason: "timeout".into(),
    }
    .into();
    assert!(err.is_db_connection_error());
    assert!(!err.is_query_syntax_error());
    assert!(!err.is_serialization_error());
    assert!(!err.is_internal());

    let inner = err
        .as_db_connection_error()
        .expect("should be DbConnectionError");
    assert_eq!(inner.reason, "timeout");
}

#[test]
fn cross_set_mapping_with_mixed_types() {
    // Target set that only has DbConnectionError.
    #[error_set]
    pub enum NarrowSet {
        DbConnectionError,
    }

    // DbConnectionError should map directly.
    let err: DataError = DbConnectionError {
        reason: "pool exhausted".into(),
    }
    .into();
    let traced_box = err.extract_inner();
    let result = NarrowSet::try_from_inner(traced_box);
    let narrow_err = result.expect("DbConnectionError should match");
    assert!(narrow_err.is_db_connection_error());

    // QuerySyntaxError should become Internal.
    let err: DataError = QuerySyntaxError { position: 10 }.into();
    let traced_box = err.extract_inner();
    let result = NarrowSet::try_from_inner(traced_box);
    let traced_box = result.unwrap_err();
    let narrow_err = NarrowSet::wrap_internal(traced_box, "narrowing");
    assert!(narrow_err.is_internal());
}

#[test]
fn question_mark_with_thiserror_type() {
    fn fallible() -> Result<(), DbConnectionError> {
        Err(DbConnectionError {
            reason: "host down".into(),
        })
    }

    fn caller() -> Result<(), DataError> {
        fallible()?;
        Ok(())
    }

    let result = caller();
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(err.is_db_connection_error());
}

#[test]
fn question_mark_with_manual_error_type() {
    fn fallible() -> Result<(), SerializationError> {
        Err(SerializationError {
            message: "bad data".into(),
        })
    }

    fn caller() -> Result<(), DataError> {
        fallible()?;
        Ok(())
    }

    let result = caller();
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(err.is_serialization_error());
}

// ---------------------------------------------------------------------------
// thiserror error with source chain
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
#[error("inner io error")]
pub struct InnerIo {
    #[source]
    cause: std::io::Error,
}

impl FfiError for InnerIo {
    fn ffi_code(&self) -> i32 {
        20
    }
}

#[error_set]
pub enum IoWrapperSet {
    InnerIo,
}

#[test]
fn thiserror_source_chain_preserved_in_error_set() {
    let io_err = std::io::Error::new(std::io::ErrorKind::NotFound, "file missing");
    let err: IoWrapperSet = InnerIo { cause: io_err }.into();

    // The error set variant should have a source.
    let source = err.source().expect("should have source");
    assert_eq!(source.to_string(), "file missing");
}
