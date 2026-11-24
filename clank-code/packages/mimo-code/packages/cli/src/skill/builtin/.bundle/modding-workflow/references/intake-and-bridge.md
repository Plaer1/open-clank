# Selected intake and bridge design

Record the selected target and version, intended change, the supported configuration or mod route, verified prerequisites, missing evidence and the next decisive observation. Read only resources the user selected and the available tools authorize. If an engine probe or native-analysis provider is unavailable, say so; do not infer that no route exists or install a replacement automatically.

Before a port or rewrite, define a small behavior oracle: concrete inputs, expected observable outputs and unchanged behavior. Make a parity matrix separating observed source and target behavior, inferred relationships and unknowns. An engine label, similar API, source code or one successful case does not establish complete compatibility. Choose the smallest useful comparison and record an actual result, including failure or partial coverage.

For two runtimes, identify the sole owner of each state and side effect. Specify versioned message fields, validation, ordering and response, plus what happens when either side restarts, a message is duplicated or delivery fails. Do not assign both sides authority for the same effect. Check one bounded end-to-end case against the oracle and describe what that case does and does not establish.
