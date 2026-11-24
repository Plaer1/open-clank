# Local fit does not prove a playable route

- WFC-style generation propagates local compatibility: choosing one tile narrows neighbors' possibilities. Contradictions require a defined restart/backtracking policy.
- A grid can satisfy every local tile rule yet isolate the exit. Flood-fill or graph search tests reachability under actual movement rules.
- Add keys, one-way doors or jump limits to the state model when they affect traversal. Geometry alone may not describe what the player can do.
- Use a bounded attempt count and a known valid fallback. An exhausted search budget means “not found within this bound,” not “impossible.”

**Self-check:** For rows `S..`, `###`, `..E`, using four-neighbor movement, state whether the exit is reachable. Change one cell to make a route, then explain why diagonal-only contact would still fail.
