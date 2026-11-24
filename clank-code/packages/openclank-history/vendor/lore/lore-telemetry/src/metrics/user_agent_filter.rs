// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use std::sync::Arc;

use rand::random;
use regex::RegexSet;
use tracing::info;

/// Recorded when a user agent was supplied but matched none of the configured patterns.
///
/// Shared by metric labels and tracing span fields so the two can be correlated; a caller that
/// substitutes its own string breaks that join silently.
pub const USER_AGENT_UNKNOWN: &str = "<unknown>";

/// Recorded when no user agent was supplied at all, in place of calling
/// [`normalize`](UserAgentFilter::normalize).
pub const USER_AGENT_NONE: &str = "<none>";

pub enum NormalizeOutput {
    KnownAgent(Arc<str>),
    Unknown,
}

/// Classifies HTTP `User-Agent` header values for use as metric labels.
///
/// When no patterns are configured (the default), every value is used as-is.
/// When patterns are configured, values that match at least one pattern are
/// used as-is; values that match none are replaced with `"<unknown>"`.
///
/// An optional [`unknown_sample_rate`](UserAgentFilter::with_unknown_sample_rate)
/// (default `0.0`) controls what fraction of unrecognised agents are recorded
/// with their actual value instead of `"<unknown>"`, allowing operators to
/// identify unexpected clients without unbounding metric cardinality.
///
/// Absent headers are recorded as `"<none>"` by the caller before calling
/// [`normalize`](UserAgentFilter::normalize).
pub struct UserAgentFilter {
    patterns: RegexSet,
    unknown_sample_rate: f64,
}

impl UserAgentFilter {
    /// Compiles `patterns` into a filter with a zero sample rate.
    ///
    /// Returns an error if any pattern is not a valid regular expression.
    pub fn new<S: AsRef<str>>(patterns: &[S]) -> Result<Self, regex::Error> {
        Ok(Self {
            patterns: RegexSet::new(patterns)?,
            unknown_sample_rate: 0.0,
        })
    }

    /// Sets the fraction of unrecognized user-agents that are sampled into
    /// the log at `info` level.
    pub fn with_unknown_sample_rate(mut self, rate: f64) -> Self {
        self.unknown_sample_rate = rate.clamp(0.0, 1.0);
        self
    }

    /// Returns the appropriate metric label for `value`.
    ///
    /// - If no patterns are configured, `value` is returned as-is.
    /// - If patterns are configured and `value` matches any of them, it is
    ///   returned as-is.
    pub fn normalize(&self, value: &str) -> NormalizeOutput {
        if self.patterns.is_empty() || self.patterns.is_match(value) {
            return NormalizeOutput::KnownAgent(Arc::from(value));
        }

        NormalizeOutput::Unknown
    }

    /// Given the user agent that is unknown, log it
    /// according to our sample rate
    pub fn sample_unknown_agent(&self, value: &str) {
        if self.unknown_sample_rate > 0.0 && random::<f64>() < self.unknown_sample_rate {
            info!(user_agent = value, "unknown user agent sampled");
        }
    }
}

impl Default for UserAgentFilter {
    fn default() -> Self {
        Self {
            patterns: RegexSet::empty(),
            unknown_sample_rate: 0.0,
        }
    }
}
