// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::future::Future;
use std::task::Poll;
use std::time::Duration;
use std::time::Instant;

use opentelemetry::KeyValue;
use opentelemetry::metrics::Histogram;
use pin_project::pin_project;

use crate::LabelArray;
use crate::METRICS_SUCCESS_ATTRIBUTE_NAME;

#[derive(Debug)]
pub struct TimedOutput<T> {
    pub output: T,
    pub elapsed: Duration,
}

#[pin_project]
pub struct ObserveFuture<T, F, FN>
where
    F: Future<Output = T>,
    // The Future Output, the Duration it took, the metric labels that are to be sent
    FN: Fn(&T, &Duration, &mut LabelArray),
{
    #[pin]
    inner: F,
    // Otel supports u64 or f64 histograms but regardless of which is used, boundaries
    // are defined as f64. So lets align with f64 like the URC Repo
    histogram: Histogram<f64>,
    labels: LabelArray,
    observe_fn: FN,
    started_timestamp: Instant,
}

impl<T, F, FN> Future for ObserveFuture<T, F, FN>
where
    F: Future<Output = T>,
    FN: Fn(&T, &Duration, &mut LabelArray),
{
    type Output = TimedOutput<T>;

    fn poll(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        let this = self.project();
        let res = this.inner.poll(cx);

        match res {
            Poll::Ready(output) => {
                let elapsed = this.started_timestamp.elapsed();
                (this.observe_fn)(&output, &elapsed, this.labels);
                this.histogram
                    .record(elapsed.as_millis() as f64, this.labels);
                Poll::Ready(TimedOutput { output, elapsed })
            }
            Poll::Pending => Poll::Pending,
        }
    }
}

/// T - the Future output
/// F - The future the returns a T
/// Observes the execution duration of the future in milliseconds
pub trait Observe<T, F, FN>
where
    F: Future<Output = T>,
    FN: Fn(&T, &Duration, &mut LabelArray),
{
    fn observe(
        self,
        histogram: Histogram<f64>,
        labels: LabelArray,
        observe_fn: FN,
    ) -> ObserveFuture<T, F, FN>;
}

impl<T, F, FN> Observe<T, F, FN> for F
where
    F: Future<Output = T>,
    FN: Fn(&T, &Duration, &mut LabelArray),
{
    fn observe(
        self,
        histogram: Histogram<f64>,
        labels: LabelArray,
        observe_fn: FN,
    ) -> ObserveFuture<T, F, FN> {
        ObserveFuture {
            inner: self,
            histogram,
            labels,
            observe_fn,
            started_timestamp: Instant::now(),
        }
    }
}

pub fn observe_result<T, E>(result: &Result<T, E>, _duration: &Duration, labels: &mut LabelArray) {
    if result.is_ok() {
        labels.push(KeyValue::new(METRICS_SUCCESS_ATTRIBUTE_NAME, true));
    } else {
        labels.push(KeyValue::new(METRICS_SUCCESS_ATTRIBUTE_NAME, false));
    }
}

pub trait ObserveResult<T, E, F>
where
    F: Future<Output = Result<T, E>>,
{
    #[allow(clippy::type_complexity)]
    fn observe_result(
        self,
        histogram: Histogram<f64>,
        labels: LabelArray,
    ) -> ObserveFuture<F::Output, F, impl Fn(&F::Output, &Duration, &mut LabelArray)>;
}

impl<T, E, F> ObserveResult<T, E, F> for F
where
    F: Future<Output = Result<T, E>>,
{
    fn observe_result(
        self,
        histogram: Histogram<f64>,
        labels: LabelArray,
    ) -> ObserveFuture<F::Output, F, impl Fn(&F::Output, &Duration, &mut LabelArray)> {
        self.observe(histogram, labels, observe_result::<T, E>)
    }
}

pub fn observe_option<T>(option: &Option<T>, _duration: &Duration, labels: &mut LabelArray) {
    if option.is_some() {
        labels.push(KeyValue::new("option", "some"));
    } else {
        labels.push(KeyValue::new("option", "none"));
    }
}

pub trait ObserveOption<T, F>
where
    F: Future<Output = Option<T>>,
{
    #[allow(clippy::type_complexity)]
    fn observe_option(
        self,
        histogram: Histogram<f64>,
        labels: LabelArray,
    ) -> ObserveFuture<F::Output, F, impl Fn(&F::Output, &Duration, &mut LabelArray)>;
}

impl<T, F> ObserveOption<T, F> for F
where
    F: Future<Output = Option<T>>,
{
    fn observe_option(
        self,
        histogram: Histogram<f64>,
        labels: LabelArray,
    ) -> ObserveFuture<F::Output, F, impl Fn(&F::Output, &Duration, &mut LabelArray)> {
        self.observe(histogram, labels, observe_option::<T>)
    }
}
