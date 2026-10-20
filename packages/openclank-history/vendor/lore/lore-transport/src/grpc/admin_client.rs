// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::sync::Arc;
use std::sync::atomic::AtomicU64;

use lore_base::lore_debug;
use lore_base::types::Address;
use lore_base::types::RepositoryId;
use lore_proto::AdminServiceClient;
use lore_proto::ObliterateRequest;

use super::AuthorizedService;
use super::AuthzInterceptor;
use super::Channel;
use super::GRPCAuthRef;
use super::RequestScopedCounter;
use super::grpc_retry;
use super::handle_error;
use crate::error::ProtocolError;

#[derive(Clone)]
pub struct AdminService {
    client: AdminServiceClient<AuthorizedService>,
    pub request_inflight: Arc<AtomicU64>,
}

impl AdminService {
    pub fn new(channel: Channel, repository: RepositoryId, auth: GRPCAuthRef) -> Self {
        let client =
            AdminServiceClient::with_interceptor(channel, AuthzInterceptor { repository, auth });

        Self {
            client,
            request_inflight: Arc::new(AtomicU64::new(0)),
        }
    }

    pub async fn obliterate(&self, address: Address) -> Result<(), ProtocolError> {
        lore_debug!("Initiating remote obliterate for address {address}");

        let mut retry = grpc_retry();
        let _response = loop {
            let _ = RequestScopedCounter::new(self.request_inflight.clone());

            let request = ObliterateRequest {
                address: Some(address.into()),
            };

            let mut client = self.client.clone();

            match client.obliterate(request).await {
                Ok(response) => {
                    break response.into_inner();
                }
                Err(status) => {
                    handle_error(&mut retry, status).await?;
                }
            }
        };

        Ok(())
    }
}
