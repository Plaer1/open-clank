// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::sync::Arc;
use std::sync::atomic::AtomicU64;

use lore_base::lore_debug;
use lore_base::types::Context;
use lore_base::types::LockData;
use lore_base::types::LockResource;
use lore_base::types::RepositoryId;
use lore_proto::lock::AdminLockRequest;
use lore_proto::lock::LockRequest;
use lore_proto::lock::QueryRequest;
use lore_proto::lock::StatusRequest;
use lore_proto::lock::UnlockRequest;
use lore_proto::lock::lock_service_client::LockServiceClient;
use tonic::Code;

use super::AuthorizedService;
use super::AuthzInterceptor;
use super::Channel;
use super::GRPCAuthRef;
use super::RequestScopedCounter;
use super::grpc_retry;
use super::handle_error;
use crate::error::ProtocolError;

#[derive(Debug, Clone)]
pub struct LockService {
    client: LockServiceClient<AuthorizedService>,
    pub request_inflight: Arc<AtomicU64>,
}

impl LockService {
    pub fn new(channel: Channel, repository: RepositoryId, auth: GRPCAuthRef) -> Self {
        let client =
            LockServiceClient::with_interceptor(channel, AuthzInterceptor { repository, auth })
                .max_decoding_message_size(32 * 1024 * 1024); // 32MiB

        Self {
            client,
            request_inflight: Arc::new(AtomicU64::new(0)),
        }
    }

    pub async fn lock(
        &self,
        resources: &[LockResource],
        owner: Option<&str>,
    ) -> Result<Vec<LockData>, ProtocolError> {
        lore_debug!("Locking resources");

        let _ = RequestScopedCounter::new(self.request_inflight.clone());

        let mut retry = grpc_retry();
        let locks = loop {
            let resources = resources.iter().map(Into::into).collect();

            if let Some(owner) = owner {
                let request = AdminLockRequest {
                    resources,
                    owner: owner.to_string(),
                };

                let mut client = self.client.clone();
                match client.admin_lock(request).await {
                    Ok(response) => {
                        break response.into_inner().locks;
                    }
                    Err(status) => handle_error(&mut retry, status).await?,
                }
            } else {
                let request = LockRequest { resources };

                let mut client = self.client.clone();
                match client.lock(request).await {
                    Ok(response) => {
                        break response.into_inner().locks;
                    }
                    Err(status) => handle_error(&mut retry, status).await?,
                }
            }
        };

        Ok(locks.into_iter().map(Into::into).collect())
    }

    pub async fn query(
        &self,
        branch: Option<Context>,
        owner: Option<&str>,
        description: Option<&str>,
    ) -> Result<Vec<LockData>, ProtocolError> {
        lore_debug!("Querying resources");

        let _ = RequestScopedCounter::new(self.request_inflight.clone());

        let mut retry = grpc_retry();
        let locks = loop {
            let request = QueryRequest {
                branch: branch.map(Context::into),
                owner: owner.map(str::to_string),
                description: description.map(str::to_string),
            };

            let mut client = self.client.clone();
            match client.query(request).await {
                Ok(response) => {
                    break response.into_inner().result;
                }
                Err(status) => handle_error(&mut retry, status).await?,
            }
        };

        Ok(locks.into_iter().map(Into::into).collect())
    }

    pub async fn status(&self, resources: &[LockResource]) -> Result<Vec<LockData>, ProtocolError> {
        lore_debug!("Fetching resource lock status");

        let _ = RequestScopedCounter::new(self.request_inflight.clone());

        let mut retry = grpc_retry();
        let locks = loop {
            let request = StatusRequest {
                resources: resources.iter().map(Into::into).collect(),
            };

            let mut client = self.client.clone();

            match client.status(request).await {
                Ok(response) => {
                    break response.into_inner().locks;
                }
                Err(status) => handle_error(&mut retry, status).await?,
            }
        };

        Ok(locks.into_iter().map(Into::into).collect())
    }

    pub async fn unlock(
        &self,
        resources: &[LockResource],
    ) -> Result<Vec<LockResource>, ProtocolError> {
        lore_debug!("Releasing resources");

        let _ = RequestScopedCounter::new(self.request_inflight.clone());

        let mut retry = grpc_retry();
        let resources = loop {
            let request = UnlockRequest {
                resources: resources.iter().map(Into::into).collect(),
            };

            let mut client = self.client.clone();

            match client.unlock(request).await {
                Ok(response) => {
                    break response.into_inner().resources;
                }
                Err(status) => {
                    if status.code() == Code::NotFound {
                        return Ok(vec![]);
                    }
                    handle_error(&mut retry, status).await?;
                }
            }
        };

        Ok(resources.into_iter().map(Into::into).collect())
    }
}
