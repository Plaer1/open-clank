// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
//! Streaming content-defined chunking.
//!
//! Cuts a file into content-defined fragments without ever holding it whole. The
//! chunker keeps a window of `2 * FRAGMENT_SIZE_THRESHOLD` bytes and only cuts while
//! at least one maximum-sized fragment is buffered, so every cut decision sees the
//! same lookahead it would with the entire file resident. The emitted boundaries are
//! therefore identical to running [`fastcdc::v2020::FastCDC`] over the whole buffer,
//! which the tests below assert directly — a boundary change would rewrite every
//! fragment hash in every repository.
//!
//! A window is half read target, half headroom for the bytes the previous cut could
//! not decide. Any file bigger than one window gets two windows: the next read runs
//! in one while chunks are cut and handed out from the other, so disk latency is off
//! the critical path, and the undecided remainder travels into the incoming window's
//! headroom so the cut logic never sees the seam. A file that fits in a single read
//! gets one window, no headroom and no read-ahead.
//!
//! A cut queues boundaries, not bytes; the chunk is copied out of the window when it
//! is handed over, which is also when the caller charges it to the fragment budget.
//! Peak residency is therefore the windows themselves, which is exactly what the
//! reservation in [`FileChunker::open`] covers.
//!
//! Each read is one `lore-io` scatter into the window it fills, so the whole scan goes
//! through the file I/O engine and no thread is held across it on a completion backend.
//! The window travels into the operation and comes back with it, which is what the
//! owned-buffer contract requires; the windowing, pipelining and cut logic above it is
//! unchanged by that, as is every cut boundary.

use std::collections::VecDeque;

use bytes::Bytes;
use bytes::BytesMut;
use tokio::task::JoinHandle;

use crate::compress::FRAGMENT_SIZE_THRESHOLD;
use crate::concurrency::FRAGMENT_SIZE_EXPECTED;
use crate::concurrency::FRAGMENT_SIZE_MINIMUM;
use crate::content::ContentHandle;
use crate::content::WindowRead;
use crate::error::StorageError;

/// Window capacity: one maximum fragment of headroom for the undecided remainder,
/// one to read into.
#[lore_macro::test_pub]
const WINDOW_SIZE: usize = 2 * FRAGMENT_SIZE_THRESHOLD;

/// A content-defined chunk: where it starts in the source, and its bytes.
pub struct Chunk {
    pub offset: u64,
    pub data: Bytes,
}

/// How the chunker picks cut points.
enum CutMode {
    /// Cut where the content says to, matching whole-file `FastCDC`.
    ContentDefined,
    /// Cut every N bytes. Never exceeds [`FRAGMENT_SIZE_THRESHOLD`], so the window
    /// always holds at least one whole chunk.
    FixedSize(usize),
}

/// A read in flight into the window that is not being cut.
#[lore_macro::test_pub]
struct PendingRead {
    task: JoinHandle<std::io::Result<BytesMut>>,
    /// The exact count the read fills, from the clamp in `start_read`. The driver reports
    /// no count of its own: an exact read either fills the request or fails.
    want: usize,
}

/// Cuts a file into chunks a window at a time.
#[lore_macro::test_pub]
pub struct FileChunker {
    handle: ContentHandle,
    /// Unconsumed bytes live in `window[head..head + length]`. Everything outside that
    /// range is uninitialised until a read or a carry-over writes it.
    window: BytesMut,
    head: usize,
    length: usize,
    /// Where a read lands in a window, and so how much room the undecided remainder
    /// has in front of it: one maximum fragment, or nothing for a single-read file.
    headroom: usize,
    /// Bytes of the window the last cut decided. Their boundaries are queued in
    /// `ready`, so they stay put until it drains and are then carried over.
    decided: usize,
    /// Boundaries cut but not yet handed out, as `(offset in window, length)`. The
    /// bytes are copied out at hand-out, so a queued boundary holds no memory.
    ready: VecDeque<(usize, usize)>,
    /// Set once a call has failed. A failed read takes its buffer with it, so the window
    /// bookkeeping no longer describes the window: resuming would cut boundaries from the
    /// wrong bytes and silently hash them as the file's.
    failed: bool,
    /// The read filling the other window while this one is handed out.
    pending: Option<PendingRead>,
    /// The other window, parked here whenever no read is in flight.
    spare: Option<BytesMut>,
    /// Absolute file offset of `window[head]`.
    base: u64,
    /// Absolute file offset the next read starts at.
    read_offset: u64,
    /// Size the file was opened at, used to spot the end when the window is sized to
    /// the file and a completely full buffer therefore also means "nothing follows".
    file_size: u64,
    eof: bool,
    mode: CutMode,
    /// Budget for every window and one chunk, released when the chunker is dropped.
    _reservation: Option<tokio::sync::OwnedSemaphorePermit>,
}

impl FileChunker {
    /// Cut on content, matching [`fastcdc::v2020::FastCDC`] over the whole file.
    #[lore_macro::test_pub]
    pub(crate) async fn content_defined(handle: ContentHandle, file_size: u64) -> Self {
        Self::open(handle, file_size, CutMode::ContentDefined).await
    }

    /// Cut every `chunk_size` bytes, clamped to a whole fragment and at least one byte
    /// so a caller passing zero cannot stall the cut loop.
    #[lore_macro::test_pub]
    pub(crate) async fn fixed_size(
        handle: ContentHandle,
        file_size: u64,
        chunk_size: usize,
    ) -> Self {
        let mode = CutMode::FixedSize(chunk_size.clamp(1, FRAGMENT_SIZE_THRESHOLD));
        Self::open(handle, file_size, mode).await
    }

    /// Reserves every window this chunker will allocate plus one maximum-size chunk
    /// from the fragment budget in a single acquire, and holds it for as long as the
    /// chunker lives.
    ///
    /// The reservation is what lets the caller finish a file it has started: the
    /// windows are held across every chunk permit the caller takes, so charging them
    /// separately would let concurrent files fill the budget with windows and then all
    /// wait on chunk permits only they could release. Because one chunk is already
    /// reserved here, a caller that cannot get a fresh permit may process a single
    /// chunk at a time without one.
    ///
    /// Every term is bounded by the file: nothing larger than the file is ever
    /// buffered or cut from it, and a file that fits in one read gets a single window
    /// with no headroom and no read-ahead.
    #[lore_macro::test_pub]
    async fn open(handle: ContentHandle, file_size: u64, mode: CutMode) -> Self {
        let capacity = file_size.min(WINDOW_SIZE as u64) as usize;
        let single_read = file_size <= WINDOW_SIZE as u64;
        let headroom = if single_read {
            0
        } else {
            FRAGMENT_SIZE_THRESHOLD
        };
        let windows = if single_read { 1 } else { 2 };
        let reserved_chunk = file_size.min(FRAGMENT_SIZE_THRESHOLD as u64) as usize;
        let reservation =
            crate::concurrency::acquire_fragment_memory_permit(windows * capacity + reserved_chunk)
                .await;

        Self {
            handle,
            // SAFETY: no byte is read before it is written. The cut only ever looks at
            // `window[head..head + length]`, which is the region the read filled plus the
            // remainder `swap_in` carries into the headroom in front of it.
            window: unsafe { lore_io::uninit_buffer(capacity) },
            head: headroom,
            length: 0,
            headroom,
            decided: 0,
            ready: VecDeque::new(),
            pending: None,
            failed: false,
            // SAFETY: as above; the two windows are used interchangeably.
            spare: (!single_read).then(|| unsafe { lore_io::uninit_buffer(capacity) }),
            base: 0,
            read_offset: 0,
            file_size,
            eof: false,
            mode,
            _reservation: reservation,
        }
    }

    /// The next chunk, or `None` once the file is exhausted.
    ///
    /// A chunker that has returned an error returns one for every later call. It cannot be
    /// resumed, and answering `None` instead would report a truncated file as complete.
    pub async fn next_chunk(&mut self) -> Result<Option<Chunk>, StorageError> {
        if self.failed {
            return Err(StorageError::internal(
                "chunker was used again after a read failure",
            ));
        }
        loop {
            if let Some((offset, length)) = self.ready.pop_front() {
                return Ok(Some(Chunk {
                    offset: self.base + (offset - self.head) as u64,
                    data: Bytes::copy_from_slice(&self.window[offset..offset + length]),
                }));
            }
            if self.eof {
                return Ok(None);
            }
            if let Err(err) = self.advance().await {
                self.failed = true;
                return Err(err);
            }
        }
    }

    /// Take the read that ran while the last batch was handed out, carry the undecided
    /// remainder in front of it, and cut again.
    async fn advance(&mut self) -> Result<(), StorageError> {
        let (buffer, read) = self.take_read().await?;
        self.swap_in(buffer, read);
        self.cut();
        self.issue_read();

        Ok(())
    }

    /// Make `buffer` the window, moving the undecided remainder into the headroom in
    /// front of the bytes just read so the two are contiguous — the cut logic never
    /// sees the seam between one read and the next.
    fn swap_in(&mut self, mut buffer: BytesMut, read: usize) {
        let remainder = self.length - self.decided;
        debug_assert!(
            remainder <= self.headroom,
            "{remainder} undecided bytes do not fit the {} byte headroom",
            self.headroom
        );

        let head = self.headroom - remainder;
        buffer[head..self.headroom]
            .copy_from_slice(&self.window[self.head + self.decided..self.head + self.length]);

        self.spare = Some(std::mem::replace(&mut self.window, buffer));
        self.base += self.decided as u64;
        self.head = head;
        self.length = remainder + read;
        self.decided = 0;
    }

    /// The bytes of the next read: from the task started while the last batch was
    /// handed out, or from one started and awaited here — which is the first read of a
    /// file, and any round where the previous cut decided nothing.
    async fn take_read(&mut self) -> Result<(BytesMut, usize), StorageError> {
        let pending = if let Some(pending) = self.pending.take() {
            pending
        } else {
            // There is no spare only in the single-read regime, where the file fits
            // in the one window and nothing has been read into it yet.
            let target = self
                .spare
                .take()
                .unwrap_or_else(|| std::mem::take(&mut self.window));
            self.start_read(target)
        };

        let buffer = pending
            .task
            .await
            .map_err(|e| StorageError::internal_with_context(e, "chunker read task failure"))?
            .map_err(|e| StorageError::internal_with_context(e, "read file for chunking"))?;

        self.read_offset += pending.want as u64;
        if self.read_offset >= self.file_size {
            self.eof = true;
        }

        Ok((buffer, pending.want))
    }

    /// Start filling the other window, so the read overlaps cutting and handing out the
    /// batch just cut. Nothing to start once the file is exhausted.
    fn issue_read(&mut self) {
        if self.pending.is_some() || self.read_offset >= self.file_size {
            return;
        }
        if let Some(target) = self.spare.take() {
            self.pending = Some(self.start_read(target));
        }
    }

    /// One scatter into `buffer[headroom..]`, spawned so it overlaps the cutting and
    /// handing out of the batch already in the other window.
    ///
    /// The length is clamped to what the file held when it was opened, so the read is an
    /// exact one: a file that shrank underneath is an error rather than a silent early
    /// end, and one appended to mid-write cannot yield a chunk list covering more bytes
    /// than the root fragment records, which readers trust.
    fn start_read(&self, buffer: BytesMut) -> PendingRead {
        let handle = self.handle.clone();
        let start = self.headroom;
        let offset = self.read_offset;
        let remaining = self.file_size.saturating_sub(offset);
        let want = (buffer.len() - start).min(remaining as usize);

        let task = lore_base::lore_spawn!(async move {
            handle
                .read_window(
                    WindowRead {
                        buffer,
                        start,
                        want,
                    },
                    offset,
                )
                .await
        });

        PendingRead { task, want }
    }

    /// Cut every chunk the current window can decide. Only boundaries are queued; the
    /// bytes stay in the window until each chunk is handed out.
    fn cut(&mut self) {
        match self.mode {
            CutMode::ContentDefined => self.cut_on_content(),
            CutMode::FixedSize(size) => self.cut_fixed(size),
        }
        debug_assert!(
            self.length - self.decided <= self.headroom,
            "undecided remainder does not fit the headroom it must be carried in"
        );
    }

    /// Queue whole fixed-size chunks, and the short tail once the file has ended.
    fn cut_fixed(&mut self, size: usize) {
        let mut decided = 0;
        while decided < self.length {
            let remaining = self.length - decided;
            let take = if remaining >= size {
                size
            } else if self.eof {
                remaining
            } else {
                break;
            };
            self.ready.push_back((self.head + decided, take));
            decided += take;
        }
        self.decided = decided;
    }

    /// Queue every content-defined cut the window can decide without more lookahead.
    fn cut_on_content(&mut self) {
        let mut chunker = fastcdc::v2020::FastCDC::with_level(
            &self.window[self.head..self.head + self.length],
            FRAGMENT_SIZE_MINIMUM as u32,
            FRAGMENT_SIZE_EXPECTED as u32,
            FRAGMENT_SIZE_THRESHOLD as u32,
            fastcdc::v2020::Normalization::Level1,
        );

        let mut decided = 0;
        loop {
            // Without a whole maximum fragment of lookahead the chunker would force a
            // cut at the window edge where the whole file would have kept scanning.
            // Tested before pulling the cut, so its scan is not computed and discarded.
            if !self.eof && decided + FRAGMENT_SIZE_THRESHOLD > self.length {
                break;
            }
            let Some(chunk) = chunker.next() else {
                break;
            };
            self.ready
                .push_back((self.head + chunk.offset, chunk.length));
            decided = chunk.offset + chunk.length;
        }
        self.decided = decided;
    }
}
