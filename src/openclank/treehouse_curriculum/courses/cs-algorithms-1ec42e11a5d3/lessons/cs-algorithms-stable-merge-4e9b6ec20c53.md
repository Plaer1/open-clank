# Merge while preserving equal-key order

- A merge consumes two already-sorted lists. Stability preserves original order among equal keys.
- Records are `(key,label)`; compare keys only. L is the left portion of the original input: `L=[(2,A),(4,B)]`, `R=[(2,C),(3,D)]`.

```text
out=[], i=0, j=0
while i<len(L) and j<len(R):
    if L[i].key <= R[j].key: append L[i]; i=i+1
    else: append R[j]; j=j+1
append remaining L[i:] then remaining R[j:]
```

**Try:** Record comparisons, output prefixes and unconsumed suffixes. Explain equality's left choice, a maintained invariant and termination. Count key comparisons/appends. Trace empty L with `R=[(1,X)]` and `L=[(1,A),(1,B)], R=[(1,C)]`. Change `<=` to `<` in the original case; give an unsorted-input counterexample to the procedure's guarantee.
