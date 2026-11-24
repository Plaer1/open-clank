// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use proc_macro::TokenStream;
use quote::quote;
use syn::Fields;
use syn::Item;
use syn::Visibility;
use syn::parse_quote;

pub fn test_pub_impl(args: TokenStream, item: TokenStream) -> TokenStream {
    if !args.is_empty() {
        let args = proc_macro2::TokenStream::from(args);
        return syn::Error::new_spanned(args, "`test_pub` takes no arguments")
            .to_compile_error()
            .into();
    }

    // Associated functions, constants and types parse as free items too.
    let original = syn::parse_macro_input!(item as Item);
    let mut widened = original.clone();
    let public: Visibility = parse_quote!(pub);

    match &mut widened {
        Item::Const(item) => item.vis = public,
        Item::Enum(item) => item.vis = public,
        Item::Fn(item) => item.vis = public,
        Item::Static(item) => item.vis = public,
        Item::Trait(item) => item.vis = public,
        Item::Type(item) => item.vis = public,
        Item::Struct(item) => {
            item.vis = public.clone();
            match &mut item.fields {
                Fields::Named(fields) => fields
                    .named
                    .iter_mut()
                    .for_each(|field| field.vis = public.clone()),
                Fields::Unnamed(fields) => fields
                    .unnamed
                    .iter_mut()
                    .for_each(|field| field.vis = public.clone()),
                Fields::Unit => {}
            }
        }
        other => {
            return syn::Error::new_spanned(
                other,
                "`test_pub` applies to a function, struct, enum, constant, static, type alias or trait",
            )
            .to_compile_error()
            .into();
        }
    }

    // The cfg is evaluated in the crate the item is in, so each crate's own
    // `test-util` feature decides.
    quote! {
        #[cfg(feature = "test-util")]
        #widened
        #[cfg(not(feature = "test-util"))]
        #original
    }
    .into()
}
