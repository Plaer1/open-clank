# A layout is a view of the graph

- Exercise input: a 3×3 board; A is a horizontal 2×1 block at (0,0), B a 1×1 block at (2,1), origin upper left. Goal: A at (1,2). One move shifts one named piece one cell orthogonally, without rotation, overlap or leaving the board. The [text board and legal-move example](#treehouse-unit=games%3Apuzzles%3Aklotski-state-and-moves) are always available.
- A's anchor has 6 possible positions; after placing A, B has 7 remaining cells. Thus this miniature has 42 valid labeled configurations before considering reachability.
- Bound exploration to 256 states and one-cell edges; deduplicate by the tuple `(Ax,Ay,Bx,By)`. Store parent pointers to reconstruct a route.
- A force-directed drawing can place related nodes near one another, but pixel distance is not move distance. Search the edges, not the image.
- An accessible explorer also needs a text board, legal-move buttons/keyboard controls, state IDs, path list, reset and a clear bound-exhausted message. No giant external viewer is needed.

**Self-check:** Specify what the explorer shows at a found goal, an exhausted reachable component and a reached search bound. Explain why zooming the picture cannot change the shortest path.
