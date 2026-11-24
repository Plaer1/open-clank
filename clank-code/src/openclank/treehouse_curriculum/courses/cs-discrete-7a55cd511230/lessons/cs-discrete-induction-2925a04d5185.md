# Induction and recursive data

- Induction proves a base case and a step from an arbitrary case k to k+1.
- For 1+…+n=n(n+1)/2, start n=0. Add k+1 to the assumed formula and simplify to (k+1)(k+2)/2.
- Structural induction follows how data is built: an empty tree, or a node with smaller subtrees.

**Try:** Prove tree_size(empty)=0 and tree_size(node(L,R))=1+size(L)+size(R) counts finite-tree nodes.
