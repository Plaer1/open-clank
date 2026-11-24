// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use opentelemetry::KeyValue;
use opentelemetry::metrics::Histogram;

/// Records a value on being dropped
pub struct DropRecord<'a> {
    histogram: Histogram<u64>,
    labels: &'a [KeyValue],

    count: u64,
}

impl<'a> DropRecord<'a> {
    pub fn new(histogram: Histogram<u64>, labels: &'a [KeyValue]) -> Self {
        Self {
            histogram,
            labels,
            count: 0,
        }
    }

    pub fn add(&mut self, value: u64) {
        self.count += value;
    }

    pub fn set(&mut self, value: u64) {
        self.count = value;
    }

    pub fn get(&self) -> u64 {
        self.count
    }
}

impl<'a> Drop for DropRecord<'a> {
    fn drop(&mut self) {
        self.histogram.record(self.count, self.labels);
    }
}
