# Greedy choices and search

- Greedy commits to a locally attractive choice; correctness needs a property that makes such choices safe.
- Backtracking explores choices, then undoes a choice when it cannot lead to a solution. Prune only with a justified rule.
- With coins {1,3,4}, greedy largest-first makes 6 as 4+1+1; 3+3 uses fewer coins.

**Try:** Show the failed greedy choice, then outline a backtracking state and a valid pruning rule for a target sum.
