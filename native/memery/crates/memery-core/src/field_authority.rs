//! Inactive pure field decisions. Provenance/review come from trusted authority.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrustedProducerContext {
    pub kind: String,
    #[serde(default)]
    pub validated_inherited_paths: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OwnerReviewContext {
    pub authenticated_owner: String,
    pub reviewer_id: String,
    pub expected_revision: i64,
    pub proposal_key: String,
    pub payload_hash: String,
    pub approved_paths: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Decision {
    pub outcome: String,
    pub classification: String,
    pub allowed_paths: Vec<String>,
    pub review_paths: Vec<String>,
    pub origins: BTreeMap<String, String>,
}

fn changes(before: Option<&Value>, after: Option<&Value>, path: &str, out: &mut Vec<(String, Option<Value>, Option<Value>)>) {
    if before == after { return; }
    if let (Some(Value::Object(left)), Some(Value::Object(right))) = (before, after) {
        let keys: BTreeSet<&String> = left.keys().chain(right.keys()).collect();
        for key in keys {
            let segment = key.replace('~', "~0").replace('/', "~1");
            changes(left.get(key), right.get(key), &format!("{path}/{segment}"), out);
        }
        return;
    }
    out.push((path.to_owned(), before.cloned(), after.cloned()));
}

fn overlaps(left: &str, right: &str) -> bool {
    left == right || left.is_empty() || right.is_empty() || left.starts_with(&format!("{right}/")) || right.starts_with(&format!("{left}/"))
}

/// Explicit before/after presence preserves absent versus JSON null in candidate payloads.
pub fn proposal_fields(current: &Value, proposed: &Value) -> Result<Vec<Value>, String> {
    if !current.is_object() || !proposed.is_object() { return Err("object values required".into()); }
    let mut delta = Vec::new();
    changes(Some(current), Some(proposed), "", &mut delta);
    let slot = |value: Option<Value>| match value {
        Some(value) => serde_json::json!({"present": true, "value": value}),
        None => serde_json::json!({"present": false}),
    };
    Ok(delta.into_iter().map(|(path, before, after)| serde_json::json!({"path":path, "before":slot(before), "after":slot(after)})).collect())
}

pub fn replay_disposition(existing_hash: Option<&str>, payload_hash: &str) -> Result<&'static str, String> {
    if payload_hash.is_empty() { return Err("payload hash required".into()); }
    Ok(match existing_hash { None => "new", Some(value) if value == payload_hash => "replay", Some(_) => "payload_conflict" })
}

#[allow(clippy::too_many_arguments)]
pub fn decide_fields(
    owner: &str, current: &Value, proposed: &Value, current_revision: i64, expected_revision: i64,
    provenance: &BTreeMap<String, String>, proposal_key: &str, payload_hash: &str,
    existing_payload_hash: Option<&str>, review: Option<&OwnerReviewContext>, classification: Option<&str>,
    producer: Option<&TrustedProducerContext>,
) -> Result<Decision, String> {
    if owner.is_empty() || !current.is_object() || !proposed.is_object() { return Err("owner and object values required".into()); }
    if provenance.iter().any(|(path, origin)| (!path.is_empty() && !path.starts_with('/')) || !matches!(origin.as_str(), "manual" | "inferred" | "inherited" | "legacy_unknown")) {
        return Err("invalid persisted field provenance".into());
    }
    if !matches!(classification, None | Some("change" | "correction" | "unresolved")) { return Err("invalid proposal classification".into()); }
    if proposal_key.is_empty() || current_revision < 1 || expected_revision < 1 { return Err("proposal identity and revisions required".into()); }
    if producer.is_some_and(|p| !matches!(p.kind.as_str(), "automatic" | "inherited" | "legacy_import") || (p.kind != "inherited" && !p.validated_inherited_paths.is_empty())) { return Err("invalid trusted producer context".into()); }
    let mut result = Decision { outcome: String::new(), classification: classification.unwrap_or("unresolved").into(), allowed_paths: vec![], review_paths: vec![], origins: BTreeMap::new() };
    if let Some(review) = review {
        if review.authenticated_owner != owner || review.reviewer_id != owner { result.outcome = "review_denied".into(); return Ok(result); }
        if review.expected_revision != current_revision { result.outcome = "stale_approval".into(); return Ok(result); }
        if review.proposal_key != proposal_key || review.payload_hash != payload_hash { result.outcome = "payload_conflict".into(); return Ok(result); }
    }
    let disposition = replay_disposition(existing_payload_hash, payload_hash)?;
    if disposition != "new" { result.outcome = disposition.into(); return Ok(result); }
    if expected_revision != current_revision { result.outcome = "stale_revision".into(); return Ok(result); }
    let mut delta = Vec::new(); changes(Some(current), Some(proposed), "", &mut delta);
    if let Some(review) = review {
        let unique: BTreeSet<_> = review.approved_paths.iter().collect();
        if unique.len() != review.approved_paths.len() || review.approved_paths.iter().any(|p| !delta.iter().any(|(path, _, _)| path == p)) {
            result.outcome = "invalid_review_subset".into(); return Ok(result);
        }
    }
    for (path, before, _) in delta {
        let present = before.is_some();
        let ancestor = provenance.iter().filter(|(key, _)| key.is_empty() || path.as_str() == key.as_str() || path.starts_with(&format!("{key}/")))
            .max_by_key(|(key, _)| key.len());
        let protected = provenance.iter().any(|(key, origin)| overlaps(&path, key) && matches!(origin.as_str(), "manual" | "legacy_unknown")) || (present && ancestor.is_none());
        let approved = review.is_some_and(|r| r.approved_paths.contains(&path));
        if (review.is_some() || protected) && !approved { result.review_paths.push(path); continue; }
        let origin = if approved { "manual" } else if !present {
            match producer {
                Some(p) if p.kind == "automatic" => "inferred",
                Some(p) if p.kind == "inherited" && p.validated_inherited_paths.contains(&path) => "inherited",
                _ => "legacy_unknown",
            }
        } else {
            ancestor.map(|(_, origin)| origin.as_str()).unwrap_or("legacy_unknown")
        };
        result.origins.insert(path.clone(), origin.into());
        result.allowed_paths.push(path);
    }
    result.outcome = if result.allowed_paths.is_empty() && result.review_paths.is_empty() { "unchanged" }
        else if !result.allowed_paths.is_empty() && !result.review_paths.is_empty() { "partial" }
        else if !result.review_paths.is_empty() { "review_required" } else { "allowed" }.into();
    Ok(result)
}

pub fn candidate_disposition(state: &str) -> Result<&'static str, String> {
    match state {
        "proposed" | "validating" | "awaiting_review" | "eligible_auto" => Ok("pending"),
        "rejected" => Ok("rejected"), "cancelled" => Ok("cancelled"),
        "activated" => Ok("activated_not_owner_approval"),
        "duplicate" | "corroborating" | "conflicted" | "quarantined" => Ok("unresolved"),
        _ => Err("unsupported candidate state".into()),
    }
}

pub fn conflict_disposition(state: &str) -> Result<&'static str, String> {
    match state { "open" => Ok("unresolved"), "resolved" => Ok("resolved_requires_audit"),
        "dismissed" => Ok("dismissed_requires_audit"), _ => Err("unsupported conflict state".into()) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Deserialize)]
    struct Input {
        owner: String,
        current: Value,
        proposed: Value,
        current_revision: i64,
        expected_revision: i64,
        provenance: BTreeMap<String, String>,
        proposal_key: String,
        payload_hash: String,
        existing_payload_hash: Option<String>,
        review: Option<OwnerReviewContext>,
        classification: Option<String>,
        producer: Option<TrustedProducerContext>,
    }

    #[test]
    fn shared_field_authority_fixtures() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../../contracts/memery/v3/s03-field-authority-fixtures.json"
        )).unwrap();
        for case in fixture["cases"].as_array().unwrap() {
            let input: Input = serde_json::from_value(case["input"].clone()).unwrap();
            let actual = decide_fields(&input.owner, &input.current, &input.proposed,
                input.current_revision, input.expected_revision, &input.provenance,
                &input.proposal_key, &input.payload_hash, input.existing_payload_hash.as_deref(),
                input.review.as_ref(), input.classification.as_deref(), input.producer.as_ref()).unwrap();
            assert_eq!(serde_json::to_value(actual).unwrap(), case["expected"], "{}", case["name"]);
            if let Some(expected) = case.get("expected_changes") {
                assert_eq!(serde_json::to_value(proposal_fields(&input.current, &input.proposed).unwrap()).unwrap(), *expected, "{}", case["name"]);
            }
        }
        use sha2::{Digest, Sha256};
        for vector in fixture["receipt_vectors"].as_array().unwrap() {
            let raw = vector["operation_json"].as_str().unwrap();
            assert_eq!(format!("{:x}", Sha256::digest(raw.as_bytes())), vector["sha256"].as_str().unwrap());
            parse_unique_json(raw).unwrap();
        }
        assert!(parse_unique_json("{\"x\":1,\"x\":2}").is_err());
        assert!(parse_unique_json("{\"x\":18446744073709551616}").is_err());
        assert!(parse_unique_json("{} {} ").is_err());
        for (state, expected) in fixture["candidate_states"].as_object().unwrap() {
            assert_eq!(candidate_disposition(state).unwrap(), expected.as_str().unwrap());
        }
        for (state, expected) in fixture["conflict_states"].as_object().unwrap() {
            assert_eq!(conflict_disposition(state).unwrap(), expected.as_str().unwrap());
        }
    }
}

/// Parse producer-sealed JSON without accepting duplicate object keys.
pub fn parse_unique_json(raw: &str) -> Result<Value, String> {
    use serde::de::{Deserialize, Deserializer, Error, MapAccess, SeqAccess, Visitor};
    use std::fmt;
    struct Unique(Value);
    struct UniqueVisitor;
    impl<'de> Visitor<'de> for UniqueVisitor {
        type Value = Unique;
        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { f.write_str("a JSON value without duplicate keys") }
        fn visit_bool<E: Error>(self, v: bool) -> Result<Unique,E> { Ok(Unique(Value::Bool(v))) }
        fn visit_i64<E: Error>(self, v: i64) -> Result<Unique,E> { Ok(Unique(Value::from(v))) }
        fn visit_u64<E: Error>(self, v: u64) -> Result<Unique,E> { Ok(Unique(Value::from(v))) }
        fn visit_f64<E: Error>(self, v: f64) -> Result<Unique,E> { serde_json::Number::from_f64(v).map(|n| Unique(Value::Number(n))).ok_or_else(|| E::custom("non-finite number")) }
        fn visit_str<E: Error>(self, v: &str) -> Result<Unique,E> { Ok(Unique(Value::String(v.to_owned()))) }
        fn visit_string<E: Error>(self, v: String) -> Result<Unique,E> { Ok(Unique(Value::String(v))) }
        fn visit_unit<E: Error>(self) -> Result<Unique,E> { Ok(Unique(Value::Null)) }
        fn visit_none<E: Error>(self) -> Result<Unique,E> { Ok(Unique(Value::Null)) }
        fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Unique,A::Error> {
            let mut values = Vec::new(); while let Some(Unique(value)) = seq.next_element()? { values.push(value); }
            Ok(Unique(Value::Array(values)))
        }
        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Unique,A::Error> {
            let mut values = serde_json::Map::new();
            while let Some(key) = map.next_key::<String>()? {
                if values.contains_key(&key) { return Err(A::Error::custom("duplicate object key")); }
                let Unique(value) = map.next_value()?; values.insert(key, value);
            }
            Ok(Unique(Value::Object(values)))
        }
    }
    impl<'de> Deserialize<'de> for Unique {
        fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self,D::Error> { d.deserialize_any(UniqueVisitor) }
    }
    // serde_json can silently convert an out-of-range integer token to f64.
    // Scan integer tokens outside strings before deserializing to fail closed.
    let bytes = raw.as_bytes();
    let mut i = 0;
    let mut quoted = false;
    let mut escaped = false;
    while i < bytes.len() {
        let byte = bytes[i];
        if quoted {
            if escaped { escaped = false; }
            else if byte == b'\\' { escaped = true; }
            else if byte == b'"' { quoted = false; }
            i += 1;
            continue;
        }
        if byte == b'"' { quoted = true; i += 1; continue; }
        if byte == b'-' || byte.is_ascii_digit() {
            let start = i;
            while i < bytes.len() && matches!(bytes[i], b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E') { i += 1; }
            let token = &raw[start..i];
            if !token.contains('.') && !token.contains('e') && !token.contains('E') && token.parse::<i64>().is_err() && token.parse::<u64>().is_err() {
                return Err("oversized integer unsupported".into());
            }
            continue;
        }
        i += 1;
    }
    let mut deserializer = serde_json::Deserializer::from_str(raw);
    let Unique(value) = Unique::deserialize(&mut deserializer).map_err(|e| e.to_string())?;
    deserializer.end().map_err(|e| e.to_string())?;
    Ok(value)
}
