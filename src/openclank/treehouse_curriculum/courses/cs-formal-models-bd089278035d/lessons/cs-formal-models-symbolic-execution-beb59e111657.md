# Path conditions

- Symbolic execution represents input as a symbol and accumulates branch constraints.
- For `if x>2: if x<5: hit()`, the hit path requires 2<x<5. The allowed domain is integers.
- Many branches can cause path explosion. Concolic testing combines concrete runs with symbolic constraints.

**Try:** Find an integer input for the path; change the second condition to x<3 and explain the result.
