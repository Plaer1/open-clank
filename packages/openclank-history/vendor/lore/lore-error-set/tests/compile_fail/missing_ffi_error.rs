// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use lore_error_set::error_set;
use std::fmt;
use std::error::Error;

#[derive(Debug)]
struct NoFfi;

impl fmt::Display for NoFfi {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "no ffi")
    }
}

impl Error for NoFfi {}
// NOTE: deliberately missing FfiError impl

#[error_set]
pub enum BadSet {
    NoFfi,
}

fn main() {}
