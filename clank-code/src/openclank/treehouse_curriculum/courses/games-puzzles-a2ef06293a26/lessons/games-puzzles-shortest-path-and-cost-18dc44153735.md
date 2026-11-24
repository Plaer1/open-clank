# A shortest path needs a move cost

- Exercise input: a 3×3 board; A is a horizontal 2×1 block at (0,0), B a 1×1 block at (2,1), origin upper left. Goal: A at (1,2). One move shifts one named piece one cell orthogonally, without rotation, overlap or leaving the board. The [text board and legal-move example](#treehouse-unit=games%3Apuzzles%3Aklotski-state-and-moves) are always available.
- Treat each valid configuration as a node and each legal one-cell move as a cost-1 edge. General BFS and queues live in Programming.
- For the miniature, moving A down, down, right reaches the goal while B stays fixed. A must change its anchor by two rows and one column, giving a three-move lower bound under this rule.
- If an unobstructed multi-cell slide costs one move instead, the same route uses two moves. “Shortest” is meaningless without the move rule and cost.
- Keep piece identities explicit. Merging interchangeable pieces is valid only when the puzzle's rules and goal treat them as equivalent.

**Self-check:** Show the intermediate states for down, down, right. Explain why this path is shortest under one-cell costs and why the conclusion changes under slide costs.
