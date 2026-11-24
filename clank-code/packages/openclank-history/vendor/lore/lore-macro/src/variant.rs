// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use proc_macro::TokenStream;
use quote::quote;
use syn::Data;
use syn::DeriveInput;
use syn::Fields;

pub fn get_variant_type_sizes(ast: &DeriveInput) -> TokenStream {
    let name = &ast.ident;
    let variants = match &ast.data {
        Data::Enum(v) => &v.variants,
        _ => panic!("VariantSizeType input is not an enum"),
    };

    let mut arms = Vec::new();

    for variant in variants {
        let ident = &variant.ident;

        let fields = &variant.fields;

        let params = match fields {
            Fields::Named(..) | Fields::Unit => continue,
            Fields::Unnamed(..) => quote! { (..) },
        };

        arms.push(quote! { #name::#ident #params => std::mem::size_of::<#fields>() });
    }

    quote! {
        impl #name {
            fn variant_size(&self) -> usize {
                match *self {
                    #(#arms),*
                }
            }
        }
    }
    .into()
}
