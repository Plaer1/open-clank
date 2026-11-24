// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! A gate that drains without holding up the holds taken during the drain.

use std::pin::pin;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;

use tokio::sync::Mutex;
use tokio::sync::Notify;

/// Counts holds per epoch, so that a drain waits for the holds taken before it and for none taken
/// after. Taking a hold never waits, whatever a drain is doing. Its use is described on
/// [`super::ImmutableStoreGroup::payload_gate`].
#[derive(Default)]
pub struct PayloadGate {
    /// Drains begun. Its parity picks the counter a hold taken now is counted in.
    epoch: AtomicUsize,
    holds: [AtomicUsize; 2],
    /// Notified when the last hold counted in a counter a drain waits on is released.
    released: Notify,
    /// Held by a drain until it is done, so that drains run one at a time and none waits on the
    /// counter new holds are taken in.
    draining: Mutex<()>,
}

/// A hold on a [`PayloadGate`], released when dropped.
#[must_use]
pub struct PayloadHold<'a> {
    gate: &'a PayloadGate,
    counter: usize,
}

impl PayloadGate {
    /// Take a hold, counted under the epoch current once it is counted: a hold that read the epoch
    /// before a drain began is counted again, under the drain's epoch. A drain then waits on every
    /// hold counted before it began and on none counted after.
    pub fn hold(&self) -> PayloadHold<'_> {
        loop {
            let epoch = self.epoch.load(Ordering::SeqCst);
            #[cfg(feature = "test-util")]
            crate::test_util::hold_interrupted();
            let counter = epoch & 1;
            self.holds[counter].fetch_add(1, Ordering::SeqCst);
            if self.epoch.load(Ordering::SeqCst) == epoch {
                return PayloadHold {
                    gate: self,
                    counter,
                };
            }
            self.release(counter);
        }
    }

    /// Wait until every hold taken before this call is released. The counter the next epoch counts
    /// holds in is emptied first, as a drain cancelled while it waited leaves its holds there, so
    /// that no hold taken before this call is counted with holds taken after.
    pub async fn drain(&self) {
        let _draining = self.draining.lock().await;
        let epoch = self.epoch.load(Ordering::SeqCst);
        self.emptied((epoch & 1) ^ 1).await;
        let counter = self.epoch.fetch_add(1, Ordering::SeqCst) & 1;
        self.emptied(counter).await;
    }

    /// Wait until no hold is counted in `counter`, which holds taken now are not counted in.
    async fn emptied(&self, counter: usize) {
        loop {
            let mut released = pin!(self.released.notified());
            released.as_mut().enable();
            if self.holds[counter].load(Ordering::SeqCst) == 0 {
                return;
            }
            released.await;
        }
    }

    /// Release a hold counted in `counter`, waking a drain waiting on it when it is the last.
    fn release(&self, counter: usize) {
        if self.holds[counter].fetch_sub(1, Ordering::SeqCst) == 1
            && self.epoch.load(Ordering::SeqCst) & 1 != counter
        {
            self.released.notify_waiters();
        }
    }
}

impl Drop for PayloadHold<'_> {
    fn drop(&mut self) {
        self.gate.release(self.counter);
    }
}

#[cfg(feature = "test-util")]
impl PayloadGate {
    /// Whether a drain is in progress.
    pub fn is_draining(&self) -> bool {
        self.draining.try_lock().is_err()
    }
}
