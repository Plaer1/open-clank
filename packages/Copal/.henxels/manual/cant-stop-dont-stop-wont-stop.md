# Manual Henxel: cant stop dont stop wont stop

This manual henxel is Copal-local. It does not live in normal hook enforcement.

Run manually:

```bash
bun run henxels:manual:cant-stop-dont-stop-wont-stop
```

Executable manual config lives at repo root:

```text
henxels.cant-stop-dont-stop-wont-stop.yaml
```

## Engagement Marker

Any plan phase that wants this manual mode engaged must include one of these exact markers:

```text
Manual Henxel: cant stop dont stop wont stop
cant-stop-dont-stop-wont-stop: engaged
```

## Active Rules

When the user explicitly launches this manual henxel, the agent treats the latest user request as the user-specified chunk. A chunk is whatever collection of slices the user said to do this time.

- No Goal mode.
- No Plan mode.
- No subagents.
- No workspace agents.
- Use one agent only.
- Do not stop after any slice.
- Do not treat a completed slice as permission to end the turn.
- Finish the full user-specified chunk before final response.
- If a blocker appears, fix or narrow it and continue; only report exact non-feasible blockers after targeted retries.
- Write notes as context grows, then continue from those notes.
- At the end of the chunk, audit all work, write a future recursive plan for remaining gaps, and execute feasible fixes from that plan before final response.

## Recursion Rule

Every future recursive plan written while this manual henxel is engaged must:

- copy this manual henxel block,
- include the command `bun run henxels:manual:cant-stop-dont-stop-wont-stop`,
- include the engagement marker,
- define chunk as the latest user-specified chunk,
- repeat one agent, no modes, no subagents, no stop after any slice,
- require the next future recursive plan to do the same.

## Boundary

This is a no self-imposed stop rule, not permission to violate system/developer/tool safety or ignore a newer explicit user pause/stop.
