# Grow a sorted prefix

- Keep a sorted prefix. Take the next record, shift prefix records with strictly greater keys right, then place it in the gap.
- Compare keys only; labels record original equal-key order.
- Input: `[(3,A),(1,B),(3,C),(2,D)]`. Begin with the first record as the prefix.

**Try:** Write the prefix after each insertion. Explain why strict-greater shifting preserves equal-key order. State the invariant, what empty/one-record input does, and why a reversed n-record list can require quadratic total shifts.
