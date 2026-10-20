# Check the index revision

- A code graph is an index of source evidence. Compare its revision with the current file before using an edge as a current fact.

**Offline revision 7:**
```python
def outer():
    return inner()
def inner():
    return 1
```
Index revision 7 records outer → inner at line 2. Current revision 9 is:
```python
def outer():
    return 1
```
That edge is stale for revision 9. An unavailable indexing backend is a separate state from an index with no matches.

A Mind outline of `# Review`, `## Decision`, `## Evidence` reflects headings; those branches are not function calls.
