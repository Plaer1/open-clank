# Calls, parameters, scope

- Parameters name a call's inputs; `return` supplies its result. Local names belong to that call.
- `def double(n): return 2*n`; calling `double(3)` returns 6. A caller's variable named n is not automatically changed.
- A function may also cause effects, such as mutating an input list; its contract should say so.

**Try:** Trace `n = 5; answer = double(n + 1)`. State n and answer and identify the function's local n.
