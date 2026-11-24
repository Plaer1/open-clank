// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! `OpenID` Connect Discovery: the provider's endpoints, read from
//! `<issuer>/.well-known/openid-configuration` and cached per issuer.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::LazyLock;

use lore_base::lore_spawn_net;
use lore_base::lore_warn;
use parking_lot::Mutex;
use serde::Deserialize;
use thiserror::Error;
use tokio::sync::OnceCell;
use tokio_util::task::AbortOnDropHandle;

use super::MAX_RESPONSE_BYTES;
use super::body_excerpt;
use super::http_client;
use super::is_permitted_url;
use super::read_capped_body;

/// The fields this client reads from a discovery document
/// (`OpenID` Connect Discovery §3, RFC 8414, RFC 8628 §4).
///
/// In a document returned by [`discover`], no URL carries a username or password, and every
/// URL is `https`, or loopback `http` when the issuer is.
#[derive(Debug, Deserialize)]
pub struct DiscoveryDocument {
    pub issuer: String,
    pub token_endpoint: String,
    pub jwks_uri: String,
    pub device_authorization_endpoint: Option<String>,
    pub revocation_endpoint: Option<String>,
    pub end_session_endpoint: Option<String>,
    pub token_endpoint_auth_methods_supported: Option<Vec<String>>,
}

/// Why a discovery document was not fetched or not accepted.
#[derive(Debug, Error)]
pub enum DiscoveryError {
    #[error("Invalid OIDC issuer URL")]
    InvalidIssuer,
    #[error("OIDC issuer '{issuer}' does not use https")]
    InsecureIssuer { issuer: String },
    /// `target_origin` is the redirect target's scheme, host and port, without the rest of
    /// the URL, which may carry a credential.
    #[error("OIDC discovery for '{issuer}' was redirected to another origin, '{target_origin}'")]
    CrossOriginRedirect {
        issuer: String,
        target_origin: String,
    },
    #[error("OIDC discovery document for '{issuer}' exceeds {MAX_RESPONSE_BYTES} bytes")]
    Oversized { issuer: String },
    #[error("OIDC discovery document from '{expected}' names the issuer '{actual}'")]
    IssuerMismatch { expected: String, actual: String },
    #[error("OIDC discovery document for '{issuer}' advertises a {field} that does not use https")]
    InsecureEndpoint { issuer: String, field: &'static str },
    #[error("OIDC discovery for '{issuer}' answered HTTP {status}")]
    Status { issuer: String, status: u16 },
    #[error("OIDC discovery document for '{issuer}' is malformed: {source}")]
    Malformed {
        issuer: String,
        source: serde_json::Error,
    },
    #[error("OIDC discovery request for '{issuer}' failed: {source}")]
    Request {
        issuer: String,
        source: reqwest::Error,
    },
    #[error("OIDC discovery task for '{issuer}' failed: {source}")]
    Task {
        issuer: String,
        source: tokio::task::JoinError,
    },
}

type CacheEntry = Arc<OnceCell<Arc<DiscoveryDocument>>>;

/// An upper bound for the discovery cache.
#[lore_macro::test_pub]
const MAX_CACHED_ISSUERS: usize = 16;

/// Discovery documents by the issuer they were fetched for. A failed fetch removes its
/// entry, and inserting into a full cache evicts the issuer least recently looked up.
#[lore_macro::test_pub]
#[derive(Default)]
struct Cache {
    /// Advances on every lookup and insert.
    clock: u64,
    slots: HashMap<String, Slot>,
}

#[lore_macro::test_pub]
struct Slot {
    /// The cache's `clock` at the last lookup of this issuer. Unique across slots.
    used: u64,
    entry: CacheEntry,
}

#[lore_macro::test_pub]
static CACHE: LazyLock<Mutex<Cache>> = LazyLock::new(Default::default);

/// The discovery document for `issuer`, fetched on the first call for that issuer and
/// cached afterwards. Concurrent calls for one issuer share a single fetch. The cache holds
/// up to [`MAX_CACHED_ISSUERS`] issuers and evicts the least recently used.
///
/// `issuer` must be an `https` URL, or plain `http` to a loopback host. The document is
/// accepted only if its `issuer` equals `issuer` exactly, and redirects are followed only
/// within the issuer's origin.
pub async fn discover(issuer: &str) -> Result<Arc<DiscoveryDocument>, DiscoveryError> {
    loop {
        let entry = cache_entry(issuer)?;
        match entry.get_or_try_init(|| fill(issuer, &entry)).await {
            Ok(document) => return Ok(document.clone()),
            Err(FillError::Removed) => {}
            Err(FillError::Failed(error)) => return Err(error),
        }
    }
}

/// `issuer`'s cache entry, inserted empty if absent. Inserting into a full cache first evicts
/// the issuer least recently looked up whose document is cached. An entry still being
/// fetched is evicted only when every entry is, so its callers are not joined by a second
/// fetch.
fn cache_entry(issuer: &str) -> Result<CacheEntry, DiscoveryError> {
    {
        let mut cache = CACHE.lock();
        let Cache { clock, slots } = &mut *cache;
        if let Some(slot) = slots.get_mut(issuer) {
            *clock += 1;
            slot.used = *clock;
            return Ok(slot.entry.clone());
        }
    }
    issuer_url(issuer)?;
    let mut cache = CACHE.lock();
    let Cache { clock, slots } = &mut *cache;
    *clock += 1;
    if slots.len() >= MAX_CACHED_ISSUERS && !slots.contains_key(issuer) {
        let oldest = slots
            .values()
            .filter(|slot| slot.entry.initialized())
            .map(|slot| slot.used)
            .min()
            .or_else(|| slots.values().map(|slot| slot.used).min());
        if let Some(oldest) = oldest {
            slots.retain(|_, slot| slot.used != oldest);
        }
    }
    let slot = slots.entry(issuer.to_owned()).or_insert_with(|| Slot {
        used: 0,
        entry: CacheEntry::default(),
    });
    slot.used = *clock;
    Ok(slot.entry.clone())
}

enum FillError {
    /// The entry left the cache before this caller could fill it.
    Removed,
    Failed(DiscoveryError),
}

/// Fetches the document into `entry`. Only one caller at a time runs this for a cell, so a
/// failed fetch removes the entry before any waiter can fetch into it again. A waiter that
/// then finds its entry removed returns [`FillError::Removed`] and looks the issuer up again.
async fn fill(issuer: &str, entry: &CacheEntry) -> Result<Arc<DiscoveryDocument>, FillError> {
    let is_current = |cache: &Cache| {
        cache
            .slots
            .get(issuer)
            .is_some_and(|slot| Arc::ptr_eq(&slot.entry, entry))
    };
    if !is_current(&CACHE.lock()) {
        return Err(FillError::Removed);
    }
    let result = fetch_on_net_runtime(issuer).await;
    if result.is_err() {
        let mut cache = CACHE.lock();
        if is_current(&cache) {
            cache.slots.remove(issuer);
        }
    }
    result.map_err(FillError::Failed)
}

/// Runs [`fetch`] on the net runtime, where network I/O runs, rather than on the caller's
/// runtime, which may be short-lived. Dropping the returned future aborts the fetch, so a
/// cancelled caller leaves no request running beside the one the next caller starts.
async fn fetch_on_net_runtime(issuer: &str) -> Result<Arc<DiscoveryDocument>, DiscoveryError> {
    let allow_loopback_http = issuer
        .get(..5)
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case("http:"));
    AbortOnDropHandle::new(lore_spawn_net!(fetch(
        issuer.to_owned(),
        allow_loopback_http
    )))
    .await
    .map_err(|source| DiscoveryError::Task {
        issuer: issuer.to_owned(),
        source,
    })?
    .map(Arc::new)
}

/// Parses `issuer`, refusing anything that may not carry requests to a provider.
fn issuer_url(issuer: &str) -> Result<url::Url, DiscoveryError> {
    let url = url::Url::parse(issuer)
        .ok()
        .filter(|url| {
            url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none()
        })
        .ok_or(DiscoveryError::InvalidIssuer)?;
    if !is_permitted_url(&url, true) {
        return Err(DiscoveryError::InsecureIssuer {
            issuer: issuer.to_owned(),
        });
    }
    Ok(url)
}

/// Fetches and checks the document for `issuer`, which [`issuer_url`] has accepted.
async fn fetch(
    issuer: String,
    allow_loopback_http: bool,
) -> Result<DiscoveryDocument, DiscoveryError> {
    let url = format!(
        "{}/.well-known/openid-configuration",
        issuer.trim_end_matches('/')
    );
    let request = match http_client() {
        Ok(client) => client.get(&url).send().await,
        Err(source) => Err(source),
    };
    let mut response = match request {
        Ok(response) => response,
        Err(source) => return Err(DiscoveryError::Request { issuer, source }),
    };

    let status = response.status();
    if status.is_redirection()
        && let Some(location) = response.headers().get(http::header::LOCATION)
        && let Ok(target) = response
            .url()
            .join(&String::from_utf8_lossy(location.as_bytes()))
        && target.origin() != response.url().origin()
    {
        let target_origin = body_excerpt(&target.origin().ascii_serialization());
        lore_warn!("OIDC discovery for {issuer} was redirected to another origin: {target_origin}");
        return Err(DiscoveryError::CrossOriginRedirect {
            issuer,
            target_origin,
        });
    }

    let body = match read_capped_body(&mut response).await {
        Ok(Some(body)) => body,
        Ok(None) => {
            lore_warn!("OIDC discovery document for {issuer} exceeds {MAX_RESPONSE_BYTES} bytes");
            return Err(DiscoveryError::Oversized { issuer });
        }
        Err(source) => return Err(DiscoveryError::Request { issuer, source }),
    };
    if !status.is_success() {
        lore_warn!(
            "OIDC discovery for {issuer} answered HTTP {}: {}",
            status.as_u16(),
            body_excerpt(&String::from_utf8_lossy(&body))
        );
        return Err(DiscoveryError::Status {
            issuer,
            status: status.as_u16(),
        });
    }

    let document: DiscoveryDocument = match serde_json::from_slice(&body) {
        Ok(document) => document,
        Err(source) => {
            lore_warn!(
                "OIDC discovery document for {issuer} is malformed: {}",
                body_excerpt(&String::from_utf8_lossy(&body))
            );
            return Err(DiscoveryError::Malformed { issuer, source });
        }
    };

    if document.issuer != issuer {
        let actual = body_excerpt(&document.issuer);
        lore_warn!("OIDC discovery document from {issuer} names the issuer {actual}");
        return Err(DiscoveryError::IssuerMismatch {
            expected: issuer,
            actual,
        });
    }

    let endpoints = [
        ("token_endpoint", Some(&document.token_endpoint)),
        ("jwks_uri", Some(&document.jwks_uri)),
        (
            "device_authorization_endpoint",
            document.device_authorization_endpoint.as_ref(),
        ),
        ("revocation_endpoint", document.revocation_endpoint.as_ref()),
        (
            "end_session_endpoint",
            document.end_session_endpoint.as_ref(),
        ),
    ];
    for (field, endpoint) in endpoints {
        let permitted = endpoint.is_none_or(|endpoint| {
            url::Url::parse(endpoint).is_ok_and(|url| is_permitted_url(&url, allow_loopback_http))
        });
        if !permitted {
            lore_warn!("OIDC discovery document for {issuer} advertises an insecure {field}");
            return Err(DiscoveryError::InsecureEndpoint { issuer, field });
        }
    }

    Ok(document)
}
