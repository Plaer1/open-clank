// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::path::Path;
use std::path::PathBuf;

use rand::distr::Alphanumeric;
use rand::distr::SampleString;

pub struct TempDir(PathBuf);

impl TempDir {
    pub fn new(prefix: &str) -> Self {
        let name = format!(
            "{prefix}{}",
            Alphanumeric.sample_string(&mut rand::rng(), 8)
        );
        let path = std::env::temp_dir().join(name);
        std::fs::create_dir_all(&path).expect("Failed to create temp directory");
        Self(path)
    }

    #[allow(dead_code)]
    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl std::ops::Deref for TempDir {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.0
    }
}

impl AsRef<Path> for TempDir {
    fn as_ref(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
