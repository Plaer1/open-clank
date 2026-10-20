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

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::RwLock;
    use std::time::Duration;

    use opentelemetry::metrics::SyncInstrument;
    use thiserror::Error;

    use super::*;

    struct RecordedValues {
        measurement: Option<f64>,
        attributes: Option<LabelArray>,
    }

    struct TestInstrument {
        recorded: RwLock<RecordedValues>,
    }

    #[derive(Debug, Error, PartialEq)]
    enum TestError {
        #[error("Something Bad")]
        SomethingBad,
    }

    impl SyncInstrument<f64> for TestInstrument {
        fn measure(&self, measurement: f64, attributes: &[KeyValue]) {
            let mut lock = self.recorded.write().unwrap();
            if lock.measurement.is_none() {
                lock.measurement = Some(measurement);
            }
            if lock.attributes.is_none() {
                let mut vec = LabelArray::new();
                vec.insert_many(0, attributes.iter().cloned());
                lock.attributes = Some(vec);
            }
        }
    }

    mod basic {
        use smallvec::smallvec;

        use super::*;

        async fn return_hello_world_with_delay(delay: u64) -> String {
            tokio::time::sleep(Duration::from_millis(delay)).await;
            "hello world".to_string()
        }

        #[tokio::test]
        async fn can_observe_future_with_closure() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_hello_world_with_delay(100).await };

            let some_local_var = Duration::from_millis(1);
            let observe_fn =
                move |_result: &String, elapsed: &Duration, labels: &mut LabelArray| {
                    if *elapsed > some_local_var {
                        labels.push(KeyValue::new("my-slow", true));
                    }
                };
            let result = task.observe(histogram, base_labels, observe_fn).await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("my-slow", true)
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert_eq!(result.output, "hello world");
        }
    }

    mod result {

        use smallvec::smallvec;

        use super::*;

        fn observe_testerror<R>(
            result: &Result<R, TestError>,
            duration: &Duration,
            labels: &mut LabelArray,
        ) {
            observe_result(result, duration, labels);
            labels.push(KeyValue::new("hello-from", "custom"));
        }

        async fn return_hello_world_with_delay(delay: u64) -> Result<String, TestError> {
            tokio::time::sleep(Duration::from_millis(delay)).await;
            Ok("hello world".to_string())
        }

        async fn return_error_with_delay(delay: u64) -> Result<String, TestError> {
            tokio::time::sleep(Duration::from_millis(delay)).await;
            Err(TestError::SomethingBad)
        }

        #[tokio::test]
        async fn can_observe_success_no_callback() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_hello_world_with_delay(100).await };

            let result = task.observe_result(histogram, base_labels).await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("success", true)
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert!(result.output.is_ok());
            assert_eq!(result.output.unwrap(), "hello world");
        }

        #[tokio::test]
        async fn can_observe_failure_no_callback() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_error_with_delay(100).await };

            let result = task.observe_result(histogram, base_labels).await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("success", false)
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert!(result.output.is_err());
            assert_eq!(result.output.unwrap_err(), TestError::SomethingBad);
        }

        #[tokio::test]
        async fn can_observe_custom_callback() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_error_with_delay(100).await };

            let result = task
                .observe(histogram, base_labels, observe_testerror)
                .await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("success", false),
                    KeyValue::new("hello-from", "custom"),
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert!(result.output.is_err());
            assert_eq!(result.output.unwrap_err(), TestError::SomethingBad);
        }
    }

    mod option {

        use smallvec::smallvec;

        use super::*;

        fn observe_test_string(
            output: &Option<String>,
            duration: &Duration,
            labels: &mut LabelArray,
        ) {
            observe_option(output, duration, labels);
            labels.push(KeyValue::new("hello-from", "custom"));
        }

        async fn return_hello_world_with_delay(delay: u64) -> Option<String> {
            tokio::time::sleep(Duration::from_millis(delay)).await;
            Some("hello world".to_string())
        }

        async fn return_none_with_delay(delay: u64) -> Option<String> {
            tokio::time::sleep(Duration::from_millis(delay)).await;
            None
        }

        #[tokio::test]
        async fn can_observe_some_no_callback() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_hello_world_with_delay(100).await };

            let result = task.observe_option(histogram, base_labels).await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("option", "some")
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert!(result.output.is_some());
            assert_eq!(result.output.unwrap(), "hello world");
        }

        #[tokio::test]
        async fn can_observe_none_no_callback() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_none_with_delay(100).await };

            let result = task.observe_option(histogram, base_labels).await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("option", "none")
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert!(result.output.is_none());
        }

        #[tokio::test]
        async fn can_observe_with_callback() {
            let recorded_values = RecordedValues {
                measurement: None,
                attributes: None,
            };
            let instrument = Arc::new(TestInstrument {
                recorded: recorded_values.into(),
            });

            let histogram = Histogram::new(instrument.clone());
            let base_labels = smallvec![KeyValue::new("Base", "Label")];

            let task = async { return_none_with_delay(100).await };

            let result = task
                .observe(histogram, base_labels, observe_test_string)
                .await;

            let read = instrument.recorded.read().unwrap();
            assert_eq!(
                read.attributes,
                Some(smallvec![
                    KeyValue::new("Base", "Label"),
                    KeyValue::new("option", "none"),
                    KeyValue::new("hello-from", "custom")
                ])
            );

            // the future was timed
            assert!(read.measurement.is_some());

            // the output is correct
            assert!(result.output.is_none());
        }
    }
}
