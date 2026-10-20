# Define a sliding puzzle precisely

- Klotski connects spatial rules to a graph of configurations. The supplied video [I Solved Klotski](https://www.youtube.com/watch?v=YGLNyHd2w10) and its [project](https://github.com/2swap/Klotski-Webpage) are inspiration; this is an original miniature, not its full puzzle graph.
- Board: 3×3 cells, origin at upper left, x rightward and y downward. A is a horizontal 2×1 block starting at (0,0); B is a 1×1 block at (2,1). Empty cells are dots.

```text
AA.
..B
...
```

- A move translates one named piece by one cell up/down/left/right, without rotation, leaving every cell inside the board and unoccupied by another piece.
- Goal: A's upper-left cell is (1,2). State stores the pieces' coordinates; a drawing is only one representation.

**Self-check:** List A's legal moves initially. Give one legal move for B and one rejected move with its violated rule.
