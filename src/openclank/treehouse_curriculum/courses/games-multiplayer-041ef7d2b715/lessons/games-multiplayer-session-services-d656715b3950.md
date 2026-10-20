# From party to match result

- A client downloads a 120 MB patch, authenticates, joins party P, queues, receives a match and region, plays a 10-minute authoritative match, stores its result, then releases capacity.
- Available roles: CDN/static delivery; account/auth; lobby/party; matchmaker; placement/allocator; game server; persistence/results; teardown/allocator.

| Player | Region R1 latency | Region R2 latency |
|---|---:|---:|
| P1 | 40 ms | 120 ms |
| P2 | 50 ms | 110 ms |
| P3 | 190 ms | 40 ms |

- Every matched player must have latency ≤100 ms. Party {P1,P2} must stay together.
- Listen hosting risks host departure; dedicated capacity adds operating work; community servers add version/moderation choices. Choose for the game’s constraints.

**Self-check:** Map all lifecycle steps to roles; choose a valid region for the party and decide whether all three can play together under the rule. Choose hosting for trusted co-op or competitive play and one downside.
