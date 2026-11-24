// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use ring::digest::SHA256;
use ring::digest::digest;
use url::ParseError;
use url::Url;

/// A short, stable fingerprint of a token, for telling one credential from
/// another without holding or logging the credential itself.
///
/// Empty for an empty token. Otherwise the leading 8 bytes of the token's
/// SHA-256 as hex: one way, so a fingerprint that reaches a log gives nothing
/// away, and wide enough that two credentials in one process will not collide.
pub fn token_fingerprint(token: &str) -> String {
    if token.is_empty() {
        return String::new();
    }

    let hash = digest(&SHA256, token.as_bytes());
    hash.as_ref()[..8]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub fn domain_from_url_or_url(url: &Url) -> String {
    url.domain()
        .or_else(|| url.host_str())
        .unwrap_or(url.as_str())
        .to_string()
}

pub fn domain_from_url_str_or_url(remote_url: &str) -> Result<String, ParseError> {
    url::Url::parse(remote_url).map(|url| domain_from_url_or_url(&url))
}

pub fn get_domain_or_empty(url_string: &str) -> String {
    domain_from_url_str_or_url(url_string).unwrap_or("".to_string())
}
