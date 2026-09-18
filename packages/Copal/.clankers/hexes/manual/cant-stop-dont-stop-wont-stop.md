# Manual Hex: cant stop dont stop wont stop

This optional contract is engaged only by explicitly running:

`bun run hexes:manual:cant-stop-dont-stop-wont-stop`

When an engaged plan contains `cant-stop-dont-stop-wont-stop: engaged`, it must
state the following operating constraints:

- No Goal mode.
- No Plan mode.
- No subagents.
- No workspace agents.
- Use one agent.
- Work one chunk at a time, including any user-specified chunk.
- Do not stop after any slice.
- Carry unfinished work into a future recursive plan.
- Copy this manual Hex block into that future plan.
