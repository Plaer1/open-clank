# Explain why a loop works

- An invariant holds at each loop check. For summing the first i items, use `total = sum(items[0:i])` and `0≤i≤n`.
- Initially i=0 and total=0. Adding items[i] and incrementing i preserves the statement.
- At i=n it gives the full sum; n−i decreases, explaining termination.

**Try:** Apply this reasoning to items=[2,−1,4]. Give initial, one middle, and final states.
