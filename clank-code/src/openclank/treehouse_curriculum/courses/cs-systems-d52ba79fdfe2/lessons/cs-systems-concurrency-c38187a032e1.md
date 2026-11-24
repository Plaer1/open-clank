# Interleavings and shared state

- Threads can interleave operations on shared state. Read-modify-write is not automatically one indivisible operation.
- Two workers both read count=0, each computes1, and both write1: one increment is lost.
- A lock or atomic operation must cover the relevant invariant; synchronization can also introduce deadlocks or waiting.

**Try:** Write the losing interleaving and propose a change that preserves both increments. Name the protected invariant.
