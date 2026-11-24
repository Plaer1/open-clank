// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Memory a read lands in, divided among the leaves and the I/O operations that write it.
//!
//! A read gives its destination to work that can outlive the read's future: an I/O operation runs
//! to completion whether or not anything still awaits it, and a leaf's task stops only at its next
//! await. So every handle to the memory holds it, and the memory is released only once the last
//! handle is gone: freed, when the read allocated it, or handed back through its [`Loan`], when a
//! caller lent it.

use std::future::Future;
use std::mem::MaybeUninit;
use std::ptr::NonNull;
use std::sync::Arc;
use std::sync::Weak;
use std::sync::atomic::Ordering;
use std::sync::atomic::fence;
use std::task::Poll;

use bytes::Bytes;
use futures::task::AtomicWaker;

/// What every handle to a buffer's memory holds, dropped with the last of them.
struct Hold {
    /// The memory, when a read allocated it rather than a caller lending it.
    owned: Option<NonNull<[MaybeUninit<u8>]>>,
    /// The lender waiting for its memory back.
    returned: AtomicWaker,
}

// SAFETY: `owned` is freed by the hold alone, once no handle can write it.
unsafe impl Send for Hold {}
unsafe impl Sync for Hold {}

impl Hold {
    fn new(owned: Option<NonNull<[MaybeUninit<u8>]>>) -> Arc<Hold> {
        Arc::new(Hold {
            owned,
            returned: AtomicWaker::new(),
        })
    }
}

impl Drop for Hold {
    fn drop(&mut self) {
        if let Some(owned) = self.owned.take() {
            // SAFETY: leaked from a box by `CallerBuffer::allocate`, and the last handle that could
            // write it is gone.
            drop(unsafe { Box::from_raw(owned.as_ptr()) });
        }
        // The last handle can drop on a backend's completion thread, under its lock, so nothing
        // here waits.
        self.returned.wake();
    }
}

/// Where a read writes: the whole of a buffer, or the part of it one leaf or one I/O operation was
/// given.
///
/// Handles to the same memory name disjoint parts of it, or the holder of one writes nothing
/// through it while the other lives. Each holds the memory, so a handle given to work that
/// outlives the read keeps the memory valid until that work lets go.
pub struct CallerBuffer {
    ptr: *mut u8,
    len: usize,
    hold: Arc<Hold>,
}

impl CallerBuffer {
    /// Lend `len` bytes at `ptr` to the reads given this buffer.
    ///
    /// # Safety
    ///
    /// `ptr` must point to `len` writable bytes that stay valid, and reach nobody else, until
    /// every handle to them is gone, which the buffer's [`Loan`] reports. A read that runs to
    /// completion has let go of them when it returns. One dropped part-way may still write them
    /// afterwards, until the I/O it started completes.
    pub unsafe fn new(ptr: *mut u8, len: usize) -> Self {
        CallerBuffer {
            ptr,
            len,
            hold: Hold::new(None),
        }
    }

    /// `len` bytes the buffer owns, unwritten, and freed once the last handle to them is gone.
    pub(crate) fn allocate(len: usize) -> Self {
        let memory = NonNull::from(Box::leak(Box::<[u8]>::new_uninit_slice(len)));
        CallerBuffer {
            ptr: memory.cast::<u8>().as_ptr(),
            len,
            hold: Hold::new(Some(memory)),
        }
    }

    /// The loan on this buffer's memory, which its lender awaits before reusing the memory.
    pub fn loan(&self) -> Loan {
        Loan(Arc::downgrade(&self.hold))
    }

    /// The capacity available, in bytes.
    pub fn len(&self) -> usize {
        self.len
    }

    /// Whether the buffer has no capacity.
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// The destination as a slice.
    pub fn as_mut_slice(&mut self) -> &mut [u8] {
        // SAFETY: the hold keeps the memory valid, and this handle alone writes this part of it.
        unsafe { std::slice::from_raw_parts_mut(self.ptr, self.len) }
    }

    /// Split at `at`, keeping `..at` and returning `at..`, as [`bytes::BytesMut::split_off`] does.
    ///
    /// The two name disjoint memory, so each can be written on its own.
    pub fn split_off(&mut self, at: usize) -> CallerBuffer {
        assert!(at <= self.len, "split index out of bounds");
        let tail = CallerBuffer {
            // SAFETY: `at` is within this buffer.
            ptr: unsafe { self.ptr.add(at) },
            len: self.len - at,
            hold: self.hold.clone(),
        };
        self.len = at;
        tail
    }

    /// A second handle to the first `len` bytes, for a read that takes its destination and gives it
    /// back.
    ///
    /// # Safety
    ///
    /// `len` is at most [`Self::len`], and nothing writes through `self` while the second handle
    /// lives.
    pub(crate) unsafe fn prefix(&mut self, len: usize) -> CallerBuffer {
        debug_assert!(len <= self.len, "prefix length out of bounds");
        CallerBuffer {
            ptr: self.ptr,
            len,
            hold: self.hold.clone(),
        }
    }

    /// The memory a read allocated, as [`Bytes`] without a copy, once no other handle to it is
    /// left. `None` while one is, for a part of the memory, and for memory a caller lent.
    ///
    /// # Safety
    ///
    /// Every byte of the memory has been written.
    pub(crate) unsafe fn into_bytes(self) -> Option<Bytes> {
        let mut hold = Arc::into_inner(self.hold)?;
        let whole = hold.owned.is_some_and(|memory| {
            memory.cast::<u8>().as_ptr() == self.ptr && memory.len() == self.len
        });
        if !whole {
            return None;
        }
        let memory = hold.owned.take()?;
        // SAFETY: leaked from a box by `allocate`, taken from the hold so it is not freed twice, and
        // written whole, as the caller guarantees.
        let memory = unsafe { Box::from_raw(memory.as_ptr()).assume_init() };
        Some(Bytes::from(memory))
    }
}

// SAFETY: the hold keeps the memory valid for as long as any handle lives, and each handle names
// memory its holder alone writes.
unsafe impl Send for CallerBuffer {}

impl lore_io::StableBufListMut for CallerBuffer {
    fn byte_segments_mut(&mut self) -> impl Iterator<Item = &mut [u8]> {
        std::iter::once(self.as_mut_slice())
    }
}

/// Lends caller memory to reads under one [`Loan`], however many buffers it lends. A clone lends
/// under the same loan.
#[derive(Clone)]
pub struct Lender(Arc<Hold>);

impl Lender {
    /// A lender that has lent nothing yet.
    pub fn new() -> Self {
        Lender(Hold::new(None))
    }

    /// Lend `len` bytes at `ptr` to the reads given the buffer returned.
    ///
    /// # Safety
    ///
    /// As [`CallerBuffer::new`], until the lender's [`Loan`] is returned.
    pub unsafe fn lend(&self, ptr: *mut u8, len: usize) -> CallerBuffer {
        CallerBuffer {
            ptr,
            len,
            hold: self.0.clone(),
        }
    }

    /// The loan on everything lent, which is not returned while the lender itself lives.
    pub fn loan(&self) -> Loan {
        Loan(Arc::downgrade(&self.0))
    }
}

impl Default for Lender {
    fn default() -> Self {
        Self::new()
    }
}

/// A lender's claim on the memory it lent, returned once every handle to the memory is gone and
/// nothing writes it any more. One waiter at a time.
pub struct Loan(Weak<Hold>);

impl Loan {
    /// Whether every handle to the memory is gone.
    pub fn is_returned(&self) -> bool {
        let returned = self.0.strong_count() == 0;
        if returned {
            // Pairs with the release of the last handle's drop, so that every write made through
            // a handle happens before the lender reuses the memory.
            fence(Ordering::Acquire);
        }
        returned
    }

    /// Wait until every handle to the memory is gone.
    pub fn returned(&self) -> impl Future<Output = ()> + '_ {
        std::future::poll_fn(|cx| {
            let Some(hold) = self.0.upgrade() else {
                // As in `is_returned`: a failed upgrade orders nothing.
                fence(Ordering::Acquire);
                return Poll::Ready(());
            };
            hold.returned.register(cx.waker());
            // Dropping this reference may be what returns the loan, which wakes the waker just
            // registered.
            drop(hold);
            Poll::Pending
        })
    }
}
