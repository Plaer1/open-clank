//! Canonical persistence-boundary privacy policy for durable memory.
//!
//! Explicit no-store material is rejected before raw capture. Known
//! credential shapes are redacted before they can reach raw, candidate,
//! curated, graph, FTS, digest, or promotion projections.

use serde_json::Value;

pub const REDACTED: &str = "[REDACTED]";
pub const NOT_STORED: &str = "[NOT STORED]";

const NO_STORE_MARKERS: &[&str] = &["<no-memory>", "[memory:no-store]"];
const SECRET_PREFIXES: &[(&str, usize)] = &[
    ("github_pat_", 24),
    ("ghp_", 20),
    ("gho_", 20),
    ("ghu_", 20),
    ("ghs_", 20),
    ("ghr_", 20),
    ("sk-proj-", 20),
    ("sk-ant-", 20),
    ("sk-", 20),
    ("xoxb-", 20),
    ("xoxp-", 20),
    ("xoxa-", 20),
    ("AIza", 24),
    ("AKIA", 20),
    ("ASIA", 20),
];

fn secret_value_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(byte, b'-' | b'_' | b'.' | b'/' | b'+' | b'=' | b':' | b'~')
}

fn redact_prefixed(mut value: String, prefix: &str, minimum_len: usize) -> (String, bool) {
    let mut changed = false;
    let mut cursor = 0;
    while cursor < value.len() {
        let Some(relative) = value[cursor..].find(prefix) else {
            break;
        };
        let start = cursor + relative;
        let bytes = value.as_bytes();
        let mut end = start + prefix.len();
        while end < bytes.len() && secret_value_byte(bytes[end]) {
            end += 1;
        }
        if end - start < minimum_len {
            cursor = start + prefix.len();
            continue;
        }
        value.replace_range(start..end, REDACTED);
        changed = true;
        cursor = start + REDACTED.len();
    }
    (value, changed)
}

fn redact_bearer(mut value: String) -> (String, bool) {
    let mut changed = false;
    let mut cursor = 0;
    loop {
        let lower = value.to_ascii_lowercase();
        let Some(relative) = lower[cursor..].find("bearer ") else {
            break;
        };
        let start = cursor + relative + "bearer ".len();
        let bytes = value.as_bytes();
        let mut end = start;
        while end < bytes.len() && secret_value_byte(bytes[end]) {
            end += 1;
        }
        if end - start < 8 {
            cursor = end.max(start + 1);
            continue;
        }
        value.replace_range(start..end, REDACTED);
        changed = true;
        cursor = start + REDACTED.len();
    }
    (value, changed)
}

fn redact_assignments(mut value: String) -> (String, bool) {
    const KEYS: &[&str] = &[
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "passwd",
        "passphrase",
        "authorization",
    ];
    let mut changed = false;
    for key in KEYS {
        let mut cursor = 0;
        loop {
            let lower = value.to_ascii_lowercase();
            let Some(relative) = lower[cursor..].find(key) else {
                break;
            };
            let key_start = cursor + relative;
            let mut start = key_start + key.len();
            let bytes = value.as_bytes();
            while start < bytes.len() && bytes[start].is_ascii_whitespace() {
                start += 1;
            }
            if start >= bytes.len() || !matches!(bytes[start], b'=' | b':') {
                cursor = key_start + key.len();
                continue;
            }
            start += 1;
            while start < bytes.len() && bytes[start].is_ascii_whitespace() {
                start += 1;
            }
            let quote = bytes
                .get(start)
                .copied()
                .filter(|byte| matches!(byte, b'"' | b'\''));
            if quote.is_some() {
                start += 1;
            }
            let mut end = start;
            while end < bytes.len()
                && match quote {
                    Some(quote) => bytes[end] != quote,
                    None => {
                        !bytes[end].is_ascii_whitespace()
                            && !matches!(bytes[end], b',' | b';' | b'}')
                    }
                }
            {
                end += 1;
            }
            if end - start < 4 {
                cursor = end.max(key_start + key.len());
                continue;
            }
            value.replace_range(start..end, REDACTED);
            changed = true;
            cursor = start + REDACTED.len();
        }
    }
    (value, changed)
}

fn must_not_store(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    NO_STORE_MARKERS.iter().any(|marker| lower.contains(marker))
        || (lower.contains("-----begin ") && lower.contains("private key-----"))
}

/// Return `None` when a value must not persist. Otherwise return the redacted
/// value and whether its body changed.
pub fn filter_text(value: &str) -> Option<(String, bool)> {
    if must_not_store(value) {
        return None;
    }
    let mut redacted = value.to_string();
    let mut changed = false;
    for (prefix, minimum_len) in SECRET_PREFIXES {
        let (next, hit) = redact_prefixed(redacted, prefix, *minimum_len);
        redacted = next;
        changed |= hit;
    }
    let (next, hit) = redact_bearer(redacted);
    redacted = next;
    changed |= hit;
    let (next, hit) = redact_assignments(redacted);
    Some((next, changed | hit))
}

fn sensitive_key(key: &str) -> bool {
    let key = key.to_ascii_lowercase().replace('-', "_");
    [
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "passphrase",
        "authorization",
        "cookie",
        "private_key",
    ]
    .iter()
    .any(|needle| key == *needle || key.ends_with(&format!("_{needle}")))
}

/// Redact a metadata tree without changing its shape. The count can be used
/// for non-sensitive metrics.
pub fn filter_json(value: &Value) -> (Value, usize) {
    match value {
        Value::Object(object) => {
            let mut hits = 0;
            let mapped = object
                .iter()
                .map(|(key, value)| {
                    if sensitive_key(key) {
                        hits += 1;
                        (key.clone(), Value::String(REDACTED.into()))
                    } else {
                        let (value, nested_hits) = filter_json(value);
                        hits += nested_hits;
                        (key.clone(), value)
                    }
                })
                .collect();
            (Value::Object(mapped), hits)
        }
        Value::Array(values) => {
            let mut hits = 0;
            let mapped = values
                .iter()
                .map(|value| {
                    let (value, nested_hits) = filter_json(value);
                    hits += nested_hits;
                    value
                })
                .collect();
            (Value::Array(mapped), hits)
        }
        Value::String(value) => match filter_text(value) {
            Some((value, changed)) => (Value::String(value), usize::from(changed)),
            None => (Value::String(NOT_STORED.into()), 1),
        },
        value => (value.clone(), 0),
    }
}

pub fn content_hash(value: &str) -> String {
    blake3::hash(value.as_bytes()).to_hex().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_canaries_without_touching_short_lookalikes() {
        let canary = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789";
        let github = "github_pat_abcdefghijklmnopqrstuvwxyz012345";
        let input = format!(
            "safe sk-note; key={canary}; gh={github}; Authorization: Bearer abcdefghijklmnop"
        );
        let (output, changed) = filter_text(&input).expect("storable");
        assert!(changed);
        assert!(!output.contains(canary));
        assert!(!output.contains(github));
        assert!(!output.contains("abcdefghijklmnop"));
        assert!(output.contains("sk-note"));
    }

    #[test]
    fn rejects_explicit_no_store_and_private_keys() {
        for input in [
            "do not save this <no-memory>",
            "[memory:no-store] private",
            "-----BEGIN PRIVATE KEY-----\nabc",
            "-----BEGIN RSA PRIVATE KEY-----\nabc",
            "-----BEGIN EC PRIVATE KEY-----\nabc",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc",
        ] {
            assert!(filter_text(input).is_none(), "{input:?}");
        }
    }

    #[test]
    fn redacts_sensitive_metadata_recursively() {
        let (metadata, hits) = filter_json(&serde_json::json!({
            "source": "safe",
            "nested": {
                "api_key": "never persist this",
                "note": "Bearer abcdefghijklmnop"
            }
        }));
        assert_eq!(hits, 2);
        assert_eq!(metadata["nested"]["api_key"], REDACTED);
        assert!(!metadata["nested"]["note"]
            .as_str()
            .unwrap()
            .contains("abcdefghijklmnop"));
    }
}
