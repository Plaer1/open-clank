// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Location capture utilities for error tracing.
//!
//! [`Location`] stores a source file path, line number, and column number,
//! with an optional context string describing the operation at that site.
//! It is used by [`Trace`](crate::traced::Trace) to record the call sites
//! where errors are created or forwarded.

use std::fmt;
use std::sync::Arc;

/// A source code location captured at an error creation or conversion site.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Location {
    /// Source file path (as provided by `file!()`).
    pub file: &'static str,
    /// Line number in the source file.
    pub line: u32,
    /// Column number in the source file.
    pub column: u32,
    /// Optional context string describing the operation at this site.
    context: Option<Arc<str>>,
}

impl Location {
    /// Creates a new `Location` without context.
    #[inline]
    pub fn new(file: &'static str, line: u32, column: u32) -> Self {
        Self {
            file,
            line,
            column,
            context: None,
        }
    }

    /// Creates a new `Location` with a context string.
    #[inline]
    pub fn with_context(file: &'static str, line: u32, column: u32, context: Arc<str>) -> Self {
        Self {
            file,
            line,
            column,
            context: Some(context),
        }
    }

    /// Returns the context string, if one was provided.
    #[inline]
    pub fn context(&self) -> Option<&str> {
        self.context.as_deref()
    }
}

impl fmt::Display for Location {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.context {
            Some(ctx) => write!(f, "{}:{} - {}", self.file, self.line, ctx),
            None => write!(f, "{}:{}:{}", self.file, self.line, self.column),
        }
    }
}

// Compile-time assertion that Location is Send + Sync.
fn _assert_location_send_sync() {
    fn _assert<T: Send + Sync>() {}
    _assert::<Location>();
}
