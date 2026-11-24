# Reuse repeated subproblems

- Dynamic programming stores results for repeated subproblems. The state must retain everything needed for future choices.
- For positive coins {1,3,4}, best(0)=0; best(s)=1+min(best(s−c)) over coins c≤s, with impossible states marked separately.
- Memoization computes on demand; tabulation fills an order where dependencies are ready.

**Try:** Fill best(0) through best(6), and explain why the state includes remaining sum.
