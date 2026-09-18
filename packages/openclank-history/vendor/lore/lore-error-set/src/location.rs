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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn location_display() {
        let loc = Location::new("src/main.rs", 42, 5);
        assert_eq!(loc.to_string(), "src/main.rs:42:5");
    }

    #[test]
    fn location_display_with_context() {
        let loc = Location::with_context("src/main.rs", 42, 5, Arc::from("loading config"));
        assert_eq!(loc.to_string(), "src/main.rs:42 - loading config");
    }

    #[test]
    fn location_clone() {
        let loc = Location::new("src/lib.rs", 1, 1);
        let loc2 = loc.clone();
        let loc3 = loc;
        assert_eq!(loc2, loc3);
    }

    #[test]
    fn location_context_accessor() {
        let loc = Location::new("src/lib.rs", 1, 1);
        assert_eq!(loc.context(), None);

        let loc2 = Location::with_context("src/lib.rs", 1, 1, Arc::from("test context"));
        assert_eq!(loc2.context(), Some("test context"));
    }
}
