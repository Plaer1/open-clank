# Vertices, edges, representations

- A graph has vertices and edges. Directed edges have an orientation; weights add costs, not direction.
- Adjacency lists store neighbors; matrices store each possible vertex pair. Sparse graphs often favor lists.
- A DAG has no directed cycle. A topological order places each prerequisite before its dependent.

**Try:** For A→B, A→C, B→D, C→D, list neighbors and a topological order. Add one edge that makes a cycle.
