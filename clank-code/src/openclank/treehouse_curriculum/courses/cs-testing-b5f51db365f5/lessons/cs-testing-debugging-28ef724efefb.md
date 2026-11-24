# Debug by testing a hypothesis

- Reproduce the smallest failure; compare actual and expected behavior. Form a hypothesis, then change one relevant thing.
- Bug: `best=0; for x in items: best=max(best,x)` claims to return the largest item. It returns 0 for [-4,-2].
- A fix must handle every nonempty list in the stated integer domain; choose the empty-input policy explicitly.

**Try:** Propose a corrected algorithm, choose an empty-input policy, and add a regression test.
