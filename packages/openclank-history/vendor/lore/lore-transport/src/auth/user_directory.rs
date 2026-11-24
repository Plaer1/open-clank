// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::sync::Arc;

use super::token_only::TokenOnlyUserService;
use super::ucs_auth::UcsAuthentication;
use super::user_service_v1::UserServiceV1;
use crate::traits::UserService;
use crate::types::EnvironmentConfig;

/// Where a server's users are looked up, and over which protocol.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum UserDirectory {
    /// `lore.user.v1.UserService` at the advertised `user_url`.
    UserService(String),
    /// The legacy `UrcAuthApi` directory RPCs at the gRPC `auth_url`.
    UrcAuth(String),
    /// No directory: the bearer's own name from the token, everyone else as
    /// their ID.
    TokenOnly,
}

impl UserDirectory {
    pub fn of(environment: &EnvironmentConfig) -> Self {
        let endpoint = environment.endpoint.as_ref();
        let advertised = |url: Option<&String>| url.filter(|url| !url.is_empty()).cloned();
        if let Some(url) = advertised(endpoint.and_then(|e| e.user_url.as_ref())) {
            Self::UserService(url)
        } else if let Some(url) = advertised(endpoint.and_then(|e| e.auth_url.as_ref())) {
            Self::UrcAuth(url)
        } else {
            Self::TokenOnly
        }
    }

    pub fn url(&self) -> &str {
        match self {
            Self::UserService(url) | Self::UrcAuth(url) => url,
            Self::TokenOnly => "",
        }
    }

    pub fn service(&self) -> Arc<dyn UserService> {
        match self {
            Self::UserService(_) => Arc::new(UserServiceV1),
            Self::UrcAuth(_) => Arc::new(UcsAuthentication),
            Self::TokenOnly => Arc::new(TokenOnlyUserService),
        }
    }
}
