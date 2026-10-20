# Trees and priorities

- A tree has a unique simple path between each pair of vertices when viewed as a connected undirected graph.
- A binary search tree orders keys by subtrees; balanced height supports logarithmic search. A badly skewed tree can have linear height.
- A min-heap keeps each parent no greater than its children. It exposes the minimum, not a fully sorted sequence.

- For this zero-based array, root is index0; children of i are2i+1 and2i+2 when present. Parent of i>0 is floor((i−1)/2). Thus indices0→1,2 and1→3,4 define this five-item tree.

**Try:** Does array [1,4,2,8,5] satisfy min-heap parent order? Can you binary-search it as a sorted array? Explain.
