# Inspect the output of a research job

- A completed job produced an output state; it did not automatically produce a correct answer. Inspect the actual sources and result.

**Offline jobs:** J1 moved queued → running → completed and returned source IDs P1/P0. P1 says current tool returns within 14 days with receipt; P0 says expired 30 days. J2 failed and returned no sources. J3 was cancelled. J1's draft answer says 30 days.

Correct J1 using P1. J2 and J3 do not support an answer from nonexistent results. A later retry is a distinct attempt.
