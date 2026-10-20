# Choose the existing worker and inspect its result

- Select a worker by identity, owner, workspace, route and recency. Asking an existing worker to continue starts new inference; use history inspection when you only need history.
- Reuse relevant context without treating old output as newly verified. Keep a specifically requested model; do not silently substitute one.

**Offline selection:** owner learner-a, current session C1, workspace W1. C9 Receipt checker uses route-small, updated 2 minutes ago. C8 Receipt checker uses route-large, updated 1 day ago. Continue the relevant existing receipt work in C9; its duplicate title alone would not identify it. Auto teacher route is unset: `No teacher model configured`.

**Pipeline cards:** each step has the task instruction “Check receipt subtotal 12 plus tax 1.” Step 1 route-small receives receipt 12+1 and outputs wrong 14. Step 2 reviewer receives the previous output alongside that task instruction and corrects it to 13. Step 3 route-small receives the previous critique and outputs 13. A pipeline passes its configured previous output, not necessarily all earlier session history.

**Job cards:** W1/J1 running, cursor 0 returns `checking receipt\n`, next cursor 17; later done exit 0, `total=13\n`. J2 failed exit 2, missing receipt. J3 failed, killed=true. W2 lookup J1 returns not found. Shell exit 0 is not automatically a Goal verification receipt. Do not automatically restart an interrupted worker.
