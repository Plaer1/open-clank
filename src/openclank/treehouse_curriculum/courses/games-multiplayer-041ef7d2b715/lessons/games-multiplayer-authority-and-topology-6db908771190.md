# Who decides the result?

- Topology describes connections: dedicated server, player-hosted listen server or peers. Authority describes whose decision is accepted; it is not the same question.
- Clients can send intent: “move right at tick 12.” The authority checks turn, speed, resources and current state before applying it.
- Ownership names who controls an entity; it need not give that client permission to set its health.
- Cooperative, competitive, asynchronous and persistent games need different timing and trust choices. Pick those requirements before a transport.

**Self-check:** Draw or list connections for a two-player game. Name the authority for position and inventory, and reject one impossible action with a specific rule.
