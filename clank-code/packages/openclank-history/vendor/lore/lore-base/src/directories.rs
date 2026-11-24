// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use directories::ProjectDirs;

pub const STORE_APP_NAME: &str = "lore";

#[cfg(target_family = "windows")]
pub const ORGANIZATION: &str = "Epic Games";

#[cfg(target_family = "unix")]
pub const ORGANIZATION: &str = "epicgames";

pub fn project_directory() -> Option<ProjectDirs> {
    ProjectDirs::from("com", ORGANIZATION, STORE_APP_NAME)
}
