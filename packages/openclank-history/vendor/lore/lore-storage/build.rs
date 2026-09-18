// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::env;
use std::path::PathBuf;
use std::str::FromStr;

fn main() {
    if env::var("CARGO_FEATURE_OODLE").is_ok() {
        let oodle_lib_dir =
            PathBuf::from_str(&env::var("OODLE_LIB_DIR").expect("OODLE_LIB_DIR not set"))
                .expect("OODLE_LIB_DIR not a path");

        let platform = env::var("CARGO_CFG_TARGET_OS").expect("No target OS set");
        let arch = env::var("CARGO_CFG_TARGET_ARCH").expect("No target arch set");
        let profile = env::var("PROFILE").expect("No profile set");

        match platform.as_str() {
            "macos" => {
                println!(
                    "cargo:rustc-link-search={}",
                    oodle_lib_dir.join("macos").display()
                );
                println!("cargo:rustc-link-lib=static=oo2coremac64");
                println!("cargo:rustc-link-lib=framework=Security");
                println!("cargo:rustc-link-lib=framework=CoreFoundation");
            }
            "linux" => {
                if arch == "aarch64" {
                    println!(
                        "cargo:rustc-link-search={}",
                        oodle_lib_dir.join("linux").join("arm64").display()
                    );
                    println!("cargo:rustc-link-lib=static=oo2corelinuxarm64");
                } else {
                    println!(
                        "cargo:rustc-link-search={}",
                        oodle_lib_dir.join("linux").join("x64").display()
                    );
                    println!("cargo:rustc-link-lib=static=oo2corelinux64");
                }
            }
            "windows" => {
                println!(
                    "cargo:rustc-link-search={}",
                    oodle_lib_dir.join("win64").display()
                );
                println!("cargo:rustc-link-lib=static=oo2core_win64");
                if profile == "debug" {
                    println!("cargo:rustc-link-lib=msvcrtd");
                }
            }
            _ => {
                panic!("Unknown platform");
            }
        }
    }
}
