# Choose a collection by its job

- A list preserves order and repeats; a tuple fixes its outer sequence; a dictionary maps unique keys to values; a set records unique members.
- `len(x)` counts elements. Sequence `x[i]` indexes from zero; dictionary `x[key]` retrieves that key. Set membership ignores duplicates; iteration order is not promised here.

```python
scores = [2, 2, 5]
pair = (2, 5)
by_id = {"ada": 5, "bea": 2}
unique = {2, 2, 5}
```

**Try:** Predict `len(scores)`, `scores[1]`, `len(unique)`, `by_id["ada"]`, `pair[0]`. Predict the errors for `pair[0]=9`, `by_id["cy"]`, `scores[3]`, and the effect of `by_id["ada"]=7`. Choose collections for ordered recent scores, latest score by player ID, unique participants and a fixed coordinate pair; justify one operation each.
