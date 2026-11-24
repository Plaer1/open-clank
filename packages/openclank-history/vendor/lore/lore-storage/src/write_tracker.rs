// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Per-operation tracker for background fragment-write tasks.
//!
//! A `WriteTracker` collects leader and follower tasks produced during a
//! commit-like operation. Every spawned task self-reports its result into
//! shared atomic/lock state when it finishes, so nothing accumulates
//! per-task in the tracker — memory stays O(in-flight) regardless of total
//! spawns. Leaders and followers use the same dispatch machinery; the names
//! reflect the caller's role (leader owns the permit/buffer, follower
//! shadows an existing in-flight leader) and carry no tracker-level
//! difference.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::OnceLock;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::task::Context;
use std::task::Poll;
use std::task::ready;

use parking_lot::Mutex;
use pin_project::pin_project;
use tokio::sync::Notify;

use crate::error::StorageError;
use crate::immutable_store::ImmutableStore;
use crate::immutable_store::claim_for_task;
use crate::local::store_lock::StoreGuard;
use crate::types::Fragment;
use crate::write_stats::FragmentWriteStats;

/// Callback invoked per stored fragment with its header and dedup status.
pub type FragmentObserver = Arc<dyn Fn(&Fragment, bool) + Send + Sync>;

/// Per-operation background-write tracker.
///
/// Construct one per top-level operation (commit, import, …). Dispatch is
/// lock-free: [`spawn_leader`](Self::spawn_leader) and
/// [`register_follower`](Self::register_follower) each take one atomic
/// increment plus a `tokio::spawn`. Shared behind `Arc<WriteTracker>` — all
/// methods take `&self`, so many concurrent dispatchers can drive the same
/// tracker without blocking each other.
///
/// [`await_all`](Self::await_all) consumes the `Arc` and waits for every
/// spawned task to finish. No task is left dangling when it returns.
#[lore_macro::test_pub]
pub struct WriteTracker {
    /// Shared with every in-flight task. The `Arc` keeps it alive past the
    /// tracker's own lifetime if a task outlives `await_all` for any reason
    /// (shouldn't happen, but is sound).
    state: Arc<SharedState>,
    on_fragment: Option<FragmentObserver>,
}

/// State shared between the tracker and every running task.
#[lore_macro::test_pub]
struct SharedState {
    /// Number of tasks that have been spawned but not yet finished.
    /// Decremented by the `DecGuard` each task holds.
    in_flight: AtomicUsize,
    /// First error observed by any task. Later errors are discarded.
    /// Mutex is only touched on the error path (rare) and once at drain.
    first_error: Mutex<Option<StorageError>>,
    /// Notified when `in_flight` transitions to 0 so `await_all` can proceed.
    idle: Notify,
    /// A claim on the store the tasks write to, taken at the first dispatch and held until the
    /// tracker and every task it spawned have ended. The tasks run on when the operation that
    /// dispatched them is dropped before it awaits them, past the span that covered it.
    claim: OnceLock<Option<StoreGuard>>,
}

impl SharedState {
    /// Keeps `error` if no task has reported one before it.
    fn record_error(&self, error: StorageError) {
        let mut slot = self.first_error.lock();
        if slot.is_none() {
            *slot = Some(error);
        }
    }
}

/// Decrements `in_flight` in its destructor so the counter returns to zero
/// even if a task panics under `panic = "unwind"`.
struct DecGuard(Arc<SharedState>);

impl Drop for DecGuard {
    fn drop(&mut self) {
        if self.0.in_flight.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.idle.notify_one();
        }
    }
}

/// A spawned leader or follower: polls its future in place and records the error it fails with.
///
/// What the write succeeds with is dropped: the tracker only asks whether the write landed, and
/// the caller that dispatched it already has the address. Not an `async` block, which would hold
/// the future twice, as its capture and again as the value it awaits. The guard counts the task in
/// flight until the task is dropped, which happens as it completes, after its error is recorded.
#[lore_macro::test_pub]
#[pin_project]
struct TrackedTask<F> {
    #[pin]
    future: F,
    guard: DecGuard,
}

impl<F, T> Future for TrackedTask<F>
where
    F: Future<Output = Result<T, StorageError>>,
{
    type Output = ();

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        let this = self.project();
        if let Err(error) = ready!(this.future.poll(cx)) {
            this.guard.0.record_error(error);
        }
        Poll::Ready(())
    }
}

impl WriteTracker {
    pub fn new() -> Self {
        Self {
            state: Arc::new(SharedState {
                in_flight: AtomicUsize::new(0),
                first_error: Mutex::new(None),
                idle: Notify::new(),
                claim: OnceLock::new(),
            }),
            on_fragment: None,
        }
    }

    /// Claims `store` for the tasks this tracker spawns, at the first dispatch: see
    /// [`SharedState::claim`]. Later dispatches find the claim taken and do nothing.
    pub async fn claim(&self, store: &Arc<dyn ImmutableStore>) {
        if self.state.claim.get().is_none() {
            let _ = self
                .state
                .claim
                .set(claim_for_task(store, store.try_join_claim()).await);
        }
    }

    /// Construct a tracker that invokes `observer` for each stored fragment.
    pub fn with_observer(observer: FragmentObserver) -> Self {
        Self {
            on_fragment: Some(observer),
            ..Self::new()
        }
    }

    /// Construct a fresh tracker carrying the same observer as `self`.
    pub fn new_like(&self) -> Self {
        Self {
            on_fragment: self.on_fragment.clone(),
            ..Self::new()
        }
    }

    /// Invoke the observer, if one is installed.
    pub fn notify_fragment(&self, fragment: &Fragment, deduplicated: bool) {
        if let Some(observer) = &self.on_fragment {
            observer(fragment, deduplicated);
        }
    }

    /// Spawn a leader task onto the shared runtime. Returns immediately.
    ///
    /// The task self-reports its result into the shared state when it
    /// finishes — the tracker does not retain a `JoinHandle` or any per-task
    /// bookkeeping, so memory is bounded by in-flight count rather than total
    /// spawns.
    pub fn spawn_leader<F, T>(&self, future: F)
    where
        F: Future<Output = Result<T, StorageError>> + Send + 'static,
    {
        self.spawn(future);
    }

    /// Spawn a follower task onto the shared runtime. Followers wait for an
    /// existing in-flight leader (potentially from another tracker) to
    /// finish, then query the store; they hold no permit or buffer.
    ///
    /// Followers are spawned rather than queued so they progress in parallel
    /// with leaders during dispatch instead of stacking up until `await_all`.
    pub fn register_follower<F, T>(&self, future: F)
    where
        F: Future<Output = Result<T, StorageError>> + Send + 'static,
    {
        self.spawn(future);
    }

    /// Spawns `future` as a [`TrackedTask`], counted in flight from here until the task ends.
    fn spawn<F, T>(&self, future: F)
    where
        F: Future<Output = Result<T, StorageError>> + Send + 'static,
    {
        self.state.in_flight.fetch_add(1, Ordering::Relaxed);
        let guard = DecGuard(Arc::clone(&self.state));
        lore_base::lore_spawn!(TrackedTask { future, guard });
    }

    /// Wait for every outstanding task to finish and return the first error
    /// observed by any of them (if any).
    ///
    /// # Ownership invariant
    ///
    /// `await_all` consumes the `Arc<WriteTracker>` and requires it to be the
    /// sole outstanding tracker handle — any extra clone held by a concurrent
    /// dispatcher would race with the drain. The uniqueness check is
    /// enforced via `Arc::into_inner`: if any other clone is live,
    /// `await_all` returns an `Err` rather than draining a partial view.
    ///
    /// Acceptable patterns:
    ///
    /// - Dispatch all work, then `await_all` (today's commit pattern).
    /// - Construct a fresh `WriteTracker` per operation; never share across
    ///   operations that overlap in time.
    ///
    /// Unsafe patterns (will return `Err`):
    ///
    /// - Sharing one `Arc<WriteTracker>` between two commits running
    ///   concurrently on different tasks.
    /// - A background task that dispatches into the tracker from a
    ///   `tokio::spawn` whose lifetime isn't joined before `await_all`.
    pub async fn await_all(self: Arc<Self>) -> Result<(), StorageError> {
        let tracker = Arc::into_inner(self).ok_or_else(|| {
            StorageError::internal(
                "WriteTracker::await_all called while another Arc handle is live",
            )
        })?;

        // Wait for every task to finish. `Notify::notified()` must be
        // created before the predicate check so a notification between the
        // check and the await isn't lost.
        loop {
            let notified = tracker.state.idle.notified();
            if tracker.state.in_flight.load(Ordering::Acquire) == 0 {
                break;
            }
            notified.await;
        }

        tracker.state.first_error.lock().take().map_or(Ok(()), Err)
    }
}

impl Default for WriteTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// What an operation gives its fragment writes: the tracker background work is
/// dispatched into, and the counters that work reports into.
///
/// The two halves are independent. A commit supplies both; a write that must
/// finish before its caller continues supplies only the counters; the default
/// supplies neither.
///
/// Cloning it clones the tracker handle, which must not outlive
/// [`WriteTracker::await_all`] — that requires its handle to be the only one left.
/// Work handed to a spawned task therefore takes [`WriteContext::stats`] alone.
#[derive(Clone, Default)]
pub struct WriteContext {
    tracker: Option<Arc<WriteTracker>>,
    stats: Option<Arc<FragmentWriteStats>>,
}

impl WriteContext {
    /// Neither tracker nor counters: the plain inline write.
    pub fn none() -> Self {
        Self::default()
    }

    /// Counters only, for a write that cannot be dispatched into a tracker.
    pub fn counted(stats: Option<Arc<FragmentWriteStats>>) -> Self {
        Self {
            tracker: None,
            stats,
        }
    }

    /// Dispatch into `tracker` and count into `stats`.
    pub fn tracked(
        tracker: Option<Arc<WriteTracker>>,
        stats: Option<Arc<FragmentWriteStats>>,
    ) -> Self {
        Self { tracker, stats }
    }

    /// The tracker to dispatch background writes into, if any.
    pub fn tracker(&self) -> Option<&Arc<WriteTracker>> {
        self.tracker.as_ref()
    }

    /// The counters to report into, owned so a spawned task can take them
    /// without also holding the tracker that awaits it.
    pub fn stats(&self) -> Option<Arc<FragmentWriteStats>> {
        self.stats.clone()
    }

    /// Report a fact to the counters, doing nothing when none were supplied. The
    /// path with no counters costs one null check.
    pub fn count(&self, record: impl FnOnce(&FragmentWriteStats)) {
        if let Some(stats) = self.stats.as_deref() {
            record(stats);
        }
    }
}
