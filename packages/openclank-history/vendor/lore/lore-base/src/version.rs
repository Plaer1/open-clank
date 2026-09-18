// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::sync::LazyLock;

pub static LORE_LIBRARY_VERSION: LazyLock<String> =
    LazyLock::new(|| env!("VERGEN_LORE_LIBRARY_VERSION_NAME").to_owned());

pub static LORE_LIBRARY_VERSION_CSTR: &str =
    concat!(env!("VERGEN_LORE_LIBRARY_VERSION_NAME"), "\0");
