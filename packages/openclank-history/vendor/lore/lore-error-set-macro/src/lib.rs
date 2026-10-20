// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
// lore-error-set-macro: proc-macro crate for lore-error-set
//
// The `#[error_set]` attribute macro transforms a simple enum declaration
// into a full error set with traced variants, From impls, accessor methods,
// ErrorSet trait, and FfiError delegation.

mod codegen;
mod derive_ffi;

use proc_macro::TokenStream;
use syn::parse_macro_input;

/// Attribute macro for defining composable error sets.
///
/// Transforms a simple enum with bare ident variants into a full error set
/// with:
/// - Variants wrapped in `Traced<T>` plus an `Internal` catch-all
/// - `From<T>` impls for each variant (with `#[track_caller]`)
/// - `std::error::Error`, `Display`, `Debug` impls
/// - `is_*()`, `as_*()`, `as_*_traced()` accessor methods
/// - `ErrorSet` trait impl for cross-set mapping
/// - `FfiError` trait impl for FFI code delegation
///
/// # Example
///
/// ```ignore
/// use lore_error_set::error_set;
///
/// #[error_set]
/// pub enum MyErrors {
///     NotFound,
///     Timeout,
/// }
/// ```
///
/// This expands to an enum with variants `NotFound(Traced<NotFound>)`,
/// `Timeout(Traced<Timeout>)`, and `Internal(Internal)`, plus all the
/// trait implementations.
#[proc_macro_attribute]
pub fn error_set(attr: TokenStream, item: TokenStream) -> TokenStream {
    let input = parse_macro_input!(item as syn::ItemEnum);

    // Parse optional attribute arguments (e.g., `#[error_set(clone)]`).
    let attr2: proc_macro2::TokenStream = attr.into();
    let derive_clone = attr2.into_iter().any(|tt| tt.to_string() == "clone");

    codegen::generate_error_set(&input, derive_clone).into()
}

/// Derive macro for `FfiError`.
///
/// Generates an `impl FfiError` that returns a constant integer code
/// specified via the `#[ffi_code(N)]` attribute.
///
/// # Example
///
/// ```ignore
/// use lore_error_set::FfiError;
///
/// #[derive(Debug, FfiError)]
/// #[ffi_code(10)]
/// struct NotFound;
/// ```
#[proc_macro_derive(FfiError, attributes(ffi_code))]
pub fn derive_ffi_error(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as syn::DeriveInput);
    derive_ffi::derive_ffi_error(&input).into()
}
