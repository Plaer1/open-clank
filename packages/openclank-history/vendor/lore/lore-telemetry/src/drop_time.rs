// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::time::Instant;

use opentelemetry::KeyValue;
use opentelemetry::metrics::Histogram;

/// Records how long until this is dropped, in milliseconds
pub struct DropTimeMs<'a> {
    histogram: Histogram<f64>,
    labels: &'a [KeyValue],

    started_timestamp: Instant,
}

impl<'a> DropTimeMs<'a> {
    pub fn new(histogram: Histogram<f64>, labels: &'a [KeyValue]) -> Self {
        Self {
            histogram,
            labels,
            started_timestamp: Instant::now(),
        }
    }
}

impl<'a> Drop for DropTimeMs<'a> {
    fn drop(&mut self) {
        let elapsed = self.started_timestamp.elapsed();
        self.histogram
            .record(elapsed.as_millis() as f64, self.labels);
    }
}
