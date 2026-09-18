// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::time::Duration;

#[macro_export]
macro_rules! timed {
    ($histogram: expr, $labels: expr, $body: expr) => {{
        let now = std::time::Instant::now();
        let result = $body;
        let elapsed = now.elapsed();

        let mut labels: LabelArray = SmallVec::new();
        labels.extend($labels.iter().cloned());
        labels.push(KeyValue::new("success", result.is_ok()));

        $histogram.record(elapsed.as_millis() as f64, &labels);

        TimedResult { result, elapsed }
    }};
}

#[derive(Debug)]
pub struct TimedResult<T, E> {
    pub result: Result<T, E>,
    pub elapsed: Duration,
}

impl<T, E> From<TimedResult<T, E>> for Result<T, E> {
    fn from(timed_result: TimedResult<T, E>) -> Self {
        timed_result.result
    }
}
