# Loops that make progress

- A loop repeats while a condition holds. Track both the result and the state driving termination.
- `i = 0; total = 0`; while `i < 3`, add i to total then increment i. Record i and total at each loop check.
- A decreasing nonnegative amount of work can justify termination; an unchanged condition may loop forever.

**Try:** Trace the example. Then explain what happens if `i = i + 1` is omitted.
