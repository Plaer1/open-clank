# Solve a tiny constraint system

- SAT asks whether Boolean constraints have a satisfying assignment. CNF is an AND of clauses, each an OR of literals.
- In `(P or Q) and (not P or Q)`, both clauses must hold; evaluate each under your chosen truth values.
- SMT adds a specified theory such as integer arithmetic. 'Unknown' from a tool is not a proof of unsatisfiability.

**Try:** Give a satisfying assignment for the formula, then decide whether integer constraints x>2 and x<3 have a solution. Compare real x.
