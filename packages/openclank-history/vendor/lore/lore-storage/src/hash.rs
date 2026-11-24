// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use core::mem::MaybeUninit;

use crate::FragmentFlags;
use crate::compress::FRAGMENT_SIZE_THRESHOLD;
use crate::compress::FragmentError;
use crate::compress::decompress;
use crate::types::Fragment;
use crate::types::Hash;

/// Hash a function name with a domain salt prefix.
pub fn hash_function(salt: &[u8], function: &str) -> Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(salt);
    hasher.update(function.as_bytes());
    hasher.finalize().as_bytes().into()
}

/// Hash a function name with a domain salt prefix and a single byte-slice argument.
pub fn hash_function_arg_slice(salt: &[u8], function: &str, arg: &[u8]) -> Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(salt);
    hasher.update(function.as_bytes());
    hasher.update(arg);
    hasher.finalize().as_bytes().into()
}

/// Hash a function name with a domain salt prefix and a single string argument.
pub fn hash_function_arg(salt: &[u8], function: &str, arg: &str) -> Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(salt);
    hasher.update(function.as_bytes());
    hasher.update(arg.as_bytes());
    hasher.finalize().as_bytes().into()
}

/// Hash a function name with a domain salt prefix and two string arguments.
pub fn hash_function_args(salt: &[u8], function: &str, first_arg: &str, second_arg: &str) -> Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(salt);
    hasher.update(function.as_bytes());
    hasher.update(first_arg.as_bytes());
    hasher.update(second_arg.as_bytes());
    hasher.finalize().as_bytes().into()
}

/// Hash a function name with a domain salt prefix and two byte-slice arguments.
pub fn hash_function_args_slice(
    salt: &[u8],
    function: &str,
    first_arg: &[u8],
    second_arg: &[u8],
) -> Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(salt);
    hasher.update(function.as_bytes());
    hasher.update(first_arg);
    hasher.update(second_arg);
    hasher.finalize().as_bytes().into()
}

/// Hash a function name with a domain salt prefix and a variable number of string arguments.
pub fn hash_function_strs_slice(salt: &[u8], function: &str, args: &[&str]) -> Hash {
    let mut hasher = blake3::Hasher::new();
    hasher.update(salt);
    hasher.update(function.as_bytes());
    for arg in args {
        hasher.update(arg.as_bytes());
    }
    hasher.finalize().as_bytes().into()
}

/// Hash a raw data slice using blake3.
pub fn hash_slice(data: &[u8]) -> Hash {
    #[cfg(feature = "test-util")]
    crate::test_util::count_hash();
    blake3::hash(data).as_bytes().into()
}

/// Hash a fragment's content if it matches the payload metadata, decompressing first if needed
pub fn hash_fragment(fragment: Fragment, data: &[u8]) -> Result<Hash, FragmentError> {
    if fragment.size_payload as usize != data.len() {
        return Err(FragmentError::internal(
            "Invalid payload size for fragment hash",
        ));
    }

    if (fragment.flags & FragmentFlags::PayloadCompressed) == 0 {
        return Ok(hash_slice(data));
    }

    debug_assert!((fragment.flags & FragmentFlags::PayloadFragmented) == 0);
    debug_assert!(fragment.size_content as usize <= FRAGMENT_SIZE_THRESHOLD);

    let (_, decompressed) = decompress(fragment, data)?;

    if fragment.size_content as usize != decompressed.len() {
        return Err(FragmentError::internal(
            "Invalid content size for fragment hash after decompression",
        ));
    }

    Ok(hash_slice(decompressed.as_ref()))
}

/// 64-bit string hash type, used for node name lookups.
pub type StringHash = u64;

/// Longest name [`hash_string`] folds without allocating, one cache line wide.
#[lore_macro::test_pub]
const HASH_STRING_STACK_BYTES: usize = 64;

/// Compute the 64-bit xxh3 hash of the lowercase form of a string.
///
/// Names up to [`HASH_STRING_STACK_BYTES`] fold in one branchless pass over a
/// stack buffer; the high bits it accumulates say whether the name is ASCII and
/// the fold usable. Testing per byte instead would stop the pass vectorizing.
pub fn hash_string(string: &str) -> StringHash {
    let bytes = string.as_bytes();
    if bytes.len() <= HASH_STRING_STACK_BYTES {
        let mut buffer = [const { MaybeUninit::<u8>::uninit() }; HASH_STRING_STACK_BYTES];
        let mut high_bits = 0u8;
        for (target, &source) in buffer.iter_mut().zip(bytes) {
            high_bits |= source;
            target.write(source.to_ascii_lowercase());
        }
        if high_bits & 0x80 == 0 {
            // SAFETY: the loop above wrote every byte of `buffer[..bytes.len()]`.
            return xxhash_rust::xxh3::xxh3_64(unsafe { buffer[..bytes.len()].assume_init_ref() });
        }
    }
    xxhash_rust::xxh3::xxh3_64(string.to_lowercase().as_bytes())
}

/// Zero-alloc xxh3 of raw string-like bytes (same digest family as [`hash_string`] without the lowercasing, distinct from the blake3 [`hash_slice`]).
pub fn hash_string_bytes(bytes: &[u8]) -> StringHash {
    xxhash_rust::xxh3::xxh3_64(bytes)
}
