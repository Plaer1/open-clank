# Breadth-first and depth-first search

- BFS uses a FIFO queue and marks a vertex when discovered, avoiding repeated insertion.
- It explores distance layers, so first discovery gives minimum edge count in an unweighted graph.
- DFS follows a branch before backtracking; it is useful for reachability, but first discovery need not be a shortest path.

**Try:** Trace BFS from A on A:[B,C], B:[D], C:[D,E], D:[], E:[]. Record queue, parents, distances.
