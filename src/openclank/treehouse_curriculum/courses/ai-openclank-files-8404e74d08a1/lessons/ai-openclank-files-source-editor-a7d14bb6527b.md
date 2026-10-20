# Make a narrow source edit

- Source mode exposes text. Highlighting does not prove compilation, execution or a language-server rename.
- Limit a replacement to the intended identifiers; comments and strings may contain the same word for a different purpose.

**Offline file count.py:**
```python
def count(rows):
    """Count rows; keep name in this comment."""
    name = len(rows)
    return name
```
Requested change: rename the local variable `name` to `row_count`, leaving the docstring unchanged. The result's two code occurrences become `row_count`; the docstring keeps `name`.

Save and inspect a diff if practicing later. No Python execution is required for this exercise.
