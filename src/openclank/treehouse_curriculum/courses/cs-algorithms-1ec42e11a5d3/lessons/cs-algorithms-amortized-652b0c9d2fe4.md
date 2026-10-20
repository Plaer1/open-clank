# Pay for occasional expensive work

- A two-stack queue pushes arrivals onto I. When O is empty, move all I items to O, then pop O.
- Each item is pushed to I, popped from I, pushed to O, and popped from O at most once each.
- Across m operations total stack work is O(m), although one dequeue can cost O(n). This is amortized, not average over random inputs.

**Try:** Trace enqueue A,B,C; dequeue twice. Count how many times A changes stacks and explain the sequence bound.
