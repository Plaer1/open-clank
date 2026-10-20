// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::Context;
use std::task::Poll;
use std::time::Instant;

use axum::extract::MatchedPath;
use http::Request;
use http::Response;
use http::header::USER_AGENT;
use pin_project::pin_project;
use pin_project::pinned_drop;
use tower::Layer;
use tower::Service;

use super::USER_AGENT_NONE;
use super::USER_AGENT_UNKNOWN;
use super::http_metrics::HttpRequestMetrics;
use super::user_agent_filter::NormalizeOutput;
use super::user_agent_filter::UserAgentFilter;

/// A `tower::Layer` that wraps the `HttpMetricsService` used to integrate with your Axum server
///
/// Example
/// ```
/// use std::sync::Arc;
/// use lore_telemetry::user_agent_filter::UserAgentFilter;
/// let filter = Arc::new(UserAgentFilter::default());
/// let metrics_layer = lore_telemetry::http_tower_layer::HttpMetricsLayer::new(filter);
/// let tower_layer = tower::ServiceBuilder::new().layer(metrics_layer);
/// let mut server = tonic::transport::Server::builder().layer(tower_layer);
/// ```
#[derive(Clone)]
pub struct HttpMetricsLayer {
    filter: Arc<UserAgentFilter>,
}

impl HttpMetricsLayer {
    pub fn new(filter: Arc<UserAgentFilter>) -> Self {
        Self { filter }
    }
}

impl<S> Layer<S> for HttpMetricsLayer {
    type Service = HttpMetricsService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        HttpMetricsService {
            service: inner,
            filter: self.filter.clone(),
        }
    }
}

/// A `tower::Service` implementation that records standard metrics for http calls
#[derive(Clone)]
pub struct HttpMetricsService<S> {
    service: S,
    filter: Arc<UserAgentFilter>,
}

impl<S, B, C> Service<Request<B>> for HttpMetricsService<S>
where
    S: Service<Request<B>, Response = Response<C>>,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = HttpMetricsFuture<S::Future>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.service.poll_ready(cx)
    }

    fn call(&mut self, req: Request<B>) -> Self::Future {
        let method = req.method().to_string();
        let path = match req.extensions().get::<MatchedPath>() {
            Some(matched_path) => matched_path.as_str().to_owned(),
            None => "unmatched_path".into(),
        };

        let user_agent = match req.headers().get(USER_AGENT).and_then(|v| v.to_str().ok()) {
            Some(v) => match self.filter.normalize(v) {
                NormalizeOutput::KnownAgent(label) => label,
                NormalizeOutput::Unknown => {
                    self.filter.sample_unknown_agent(v);
                    USER_AGENT_UNKNOWN.clone()
                }
            },
            None => USER_AGENT_NONE.clone(),
        };

        let f = self.service.call(req);

        HttpMetricsFuture::new(&method, &path, user_agent, f)
    }
}

/// A `Future` that handles the lifetime of the http request and tracks the metrics for it
#[pin_project(PinnedDrop)]
pub struct HttpMetricsFuture<F> {
    metrics: HttpRequestMetrics,
    started_at: Option<Instant>,
    #[pin]
    inner: F,
}

impl<F> HttpMetricsFuture<F> {
    pub fn new(method: &str, path: &str, user_agent: Arc<str>, inner: F) -> Self {
        Self {
            started_at: None,
            inner,
            metrics: HttpRequestMetrics::new(method, path, user_agent),
        }
    }
}

impl<F, B, E> Future for HttpMetricsFuture<F>
where
    F: Future<Output = Result<Response<B>, E>>,
{
    type Output = F::Output;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.project();

        let started_at = this.started_at.get_or_insert_with(|| {
            this.metrics.request_started();
            Instant::now()
        });

        if let Poll::Ready(response) = this.inner.poll(cx) {
            let status_code = response.as_ref().ok().map(Response::status);

            let elapsed_seconds = Instant::now().duration_since(*started_at).as_secs_f64();
            this.metrics.request_complete(elapsed_seconds, status_code);

            Poll::Ready(response)
        } else {
            Poll::Pending
        }
    }
}

#[pinned_drop]
impl<F> PinnedDrop for HttpMetricsFuture<F> {
    fn drop(self: Pin<&mut Self>) {
        let this = self.project();

        if this.started_at.is_some() {
            this.metrics.request_finished();
        }
    }
}
