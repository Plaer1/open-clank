# Tests that challenge a claim

- Contract: first_index(items,x) returns the first matching index, or None if absent, without mutating items.
- Test partitions with distinct behavior: empty, one item, duplicates, absent target, boundary values, and invalid input when supported.
- For first_index([2,2],2), expected 0 catches a last-match implementation.
- Passing examples are evidence about tested cases; they are not a universal correctness proof.

**Try:** Write three high-value tests for first_index, including empty input and duplicates, with expected outcomes.
