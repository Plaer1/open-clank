// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use async_trait::async_trait;
use lore_base::lore_debug;
use lore_base::types::RepositoryId;
use lore_proto::lore::user::v1::User;
use lore_proto::lore::user::v1::UserFindRequest;
use lore_proto::lore::user::v1::UserGetRequest;
use lore_proto::lore::user::v1::user_find_request::Query;
use lore_proto::lore::user::v1::user_service_client::UserServiceClient;

use super::ucs_auth::connect_channel;
use super::ucs_auth::set_auth_header;
use crate::error::ProtocolError;
use crate::grpc::CorrelationInterceptor;
use crate::traits::UserService;
use crate::types::ResolvedUser;

pub struct UserServiceV1;

type Client = UserServiceClient<
    tonic::codegen::InterceptedService<tonic::transport::Channel, CorrelationInterceptor>,
>;

async fn connect_client(user_url: &str) -> Result<Client, ProtocolError> {
    Ok(UserServiceClient::with_interceptor(
        connect_channel(user_url).await?,
        CorrelationInterceptor,
    ))
}

fn resolved(user: User) -> ResolvedUser {
    let user_name = if user.display_name.is_empty() {
        user.username
    } else {
        user.display_name
    };
    ResolvedUser {
        user_id: user.id,
        user_name,
    }
}

async fn find(
    client: &mut Client,
    authz_token: &str,
    repository: RepositoryId,
    query: Query,
) -> Result<Vec<User>, ProtocolError> {
    let mut request = tonic::Request::new(UserFindRequest {
        partition: repository.into(),
        query: Some(query),
    });
    set_auth_header(&mut request, authz_token)?;
    Ok(client
        .user_find(request)
        .await
        .map_err(ProtocolError::from)?
        .into_inner()
        .user)
}

#[async_trait]
impl UserService for UserServiceV1 {
    async fn get_user_info(
        &self,
        user_url: &str,
        authz_token: &str,
        repository: RepositoryId,
        user_ids: &[String],
        _correlation_id: &str,
    ) -> Result<Vec<ResolvedUser>, ProtocolError> {
        let mut client = connect_client(user_url).await?;
        let mut request = tonic::Request::new(UserGetRequest {
            partition: repository.into(),
            id: user_ids.to_vec(),
        });
        set_auth_header(&mut request, authz_token)?;
        let users = client
            .user_get(request)
            .await
            .map_err(ProtocolError::from)?
            .into_inner()
            .user;
        Ok(users.into_iter().map(resolved).collect())
    }

    /// Several matches resolve to `None`, so a display-name collision never
    /// picks a person.
    async fn get_user_id(
        &self,
        user_url: &str,
        authz_token: &str,
        repository: RepositoryId,
        display_name: &str,
        _correlation_id: &str,
    ) -> Result<Option<ResolvedUser>, ProtocolError> {
        let mut client = connect_client(user_url).await?;
        let queries = [
            Query::Username(display_name.to_string()),
            Query::DisplayName(display_name.to_string()),
        ];
        for query in queries {
            let mut users = find(&mut client, authz_token, repository, query).await?;
            match users.len() {
                0 => {}
                1 => return Ok(users.pop().map(resolved)),
                _ => {
                    let candidates: Vec<&str> = users.iter().map(|u| u.id.as_str()).collect();
                    lore_debug!(
                        "{display_name} matches several users ({}), resolving to none",
                        candidates.join(", ")
                    );
                    return Ok(None);
                }
            }
        }
        Ok(None)
    }
}
