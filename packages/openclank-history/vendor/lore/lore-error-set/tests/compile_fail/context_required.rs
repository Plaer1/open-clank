// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use lore_error_set::FfiError;
use lore_error_set::error_set;
use lore_error_set::prelude::*;
use std::error::Error;
use std::fmt;

#[derive(Debug)]
struct Oops;

impl fmt::Display for Oops {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "oops")
    }
}

impl Error for Oops {}

impl FfiError for Oops {
    fn ffi_code(&self) -> i32 { 1 }
}

#[error_set]
pub enum SetA {
    Oops,
}

#[error_set]
pub enum SetB {
    Oops,
}

fn try_forward_no_context() -> Result<(), SetB> {
    let result: Result<(), SetA> = Err(Oops.into());
    // This should fail to compile: forward() requires a &str context argument.
    result.forward()
}

fn main() {}
