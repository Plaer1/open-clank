// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::borrow::Cow;
use std::sync::Arc;
use std::sync::LazyLock;
use std::sync::OnceLock;

use http::StatusCode;
use opentelemetry::KeyValue;
use opentelemetry::metrics::Histogram;
use opentelemetry::metrics::Meter;
use opentelemetry::metrics::UpDownCounter;
use opentelemetry_semantic_conventions::attribute::USER_AGENT_NAME;
use opentelemetry_semantic_conventions::metric::HTTP_SERVER_ACTIVE_REQUESTS;
use opentelemetry_semantic_conventions::metric::HTTP_SERVER_REQUEST_DURATION;
use opentelemetry_semantic_conventions::trace::HTTP_REQUEST_METHOD;
use opentelemetry_semantic_conventions::trace::HTTP_RESPONSE_STATUS_CODE;
use opentelemetry_semantic_conventions::trace::HTTP_ROUTE;

use super::meter;

const METER_SCOPE: &str = "http";
static METER: OnceLock<Meter> = OnceLock::new();

fn seconds_histogram(name: impl Into<Cow<'static, str>>) -> Histogram<f64> {
    METER
        .get_or_init(|| meter(METER_SCOPE))
        .f64_histogram(name)
        .with_boundaries(vec![
            0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 1.2, 2.0, 5.0, 8.0, 10.0, 15.0, 20.0, 32.0, 40.0,
            60.0,
        ])
        .with_unit("s")
        .build()
}

fn up_down_counter(name: impl Into<Cow<'static, str>>) -> UpDownCounter<i64> {
    METER
        .get_or_init(|| meter(METER_SCOPE))
        .i64_up_down_counter(name)
        .build()
}

// Http metrics
pub static HTTP_SERVER_ACTIVE_REQUESTS_METRIC: LazyLock<UpDownCounter<i64>> =
    LazyLock::new(|| up_down_counter(HTTP_SERVER_ACTIVE_REQUESTS));
pub static HTTP_SERVER_REQUEST_DURATION_METRIC: LazyLock<Histogram<f64>> =
    LazyLock::new(|| seconds_histogram(HTTP_SERVER_REQUEST_DURATION));

pub(crate) struct HttpRequestMetrics {
    method: KeyValue,
    path: KeyValue,
    user_agent: KeyValue,
}

/// Helper struct to track the lifetime metrics for the request
impl HttpRequestMetrics {
    pub fn new(method: &str, path: &str, user_agent: Arc<str>) -> Self {
        let mut path = path;
        if path.starts_with("/") {
            path = &path[1..];
        }

        Self {
            method: KeyValue::new(HTTP_REQUEST_METHOD, Arc::from(method)),
            path: KeyValue::new(HTTP_ROUTE, Arc::from(path)),
            user_agent: KeyValue::new(USER_AGENT_NAME, user_agent),
        }
    }

    pub fn request_started(&self) {
        HTTP_SERVER_ACTIVE_REQUESTS_METRIC.add(
            1,
            &[
                self.method.clone(),
                self.path.clone(),
                self.user_agent.clone(),
            ],
        );
    }

    pub fn request_complete(&self, elapsed_seconds: f64, status: Option<StatusCode>) {
        let status =
            status.map(|status| KeyValue::new(HTTP_RESPONSE_STATUS_CODE, format!("{status:?}")));

        if let Some(status) = status {
            HTTP_SERVER_REQUEST_DURATION_METRIC.record(
                elapsed_seconds,
                &[
                    self.method.clone(),
                    self.path.clone(),
                    self.user_agent.clone(),
                    status,
                ],
            );
        } else {
            HTTP_SERVER_REQUEST_DURATION_METRIC.record(
                elapsed_seconds,
                &[
                    self.method.clone(),
                    self.path.clone(),
                    self.user_agent.clone(),
                ],
            );
        }
    }

    pub fn request_finished(&self) {
        HTTP_SERVER_ACTIVE_REQUESTS_METRIC.add(
            -1,
            &[
                self.method.clone(),
                self.path.clone(),
                self.user_agent.clone(),
            ],
        );
    }
}
