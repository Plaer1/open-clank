# Specify a function

- A contract states accepted inputs, promised results, and effects/errors. It tells a caller what can be relied on.
- `first_index(items, x)` returns the first matching index, or None if absent; input is not mutated.
- A precondition restricts callers. A postcondition describes a successful result; it is not established merely by writing it.

**Try:** Specify first_index for empty input, duplicates, and absence; give one test for each.
