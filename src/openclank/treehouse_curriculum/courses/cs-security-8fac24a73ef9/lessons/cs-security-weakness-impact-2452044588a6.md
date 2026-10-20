# Weakness, vulnerability, consequence

- A weakness is a failure pattern; a vulnerability is a concrete exploitable instance under relevant conditions. Impact describes what can happen.
- Untrusted data becoming query/command syntax is injection; a data/control boundary failed.
- A crash demonstrates a failure. It does not alone establish arbitrary code execution or the reachable attack path.

**Fictional incident card — parser-crash-v1.** All observations below are supplied teaching data, not a run you performed.

- Owned target: TinyCounts-native0.1 in an isolated local test harness; no network/service or real user data.
- Input: ASCII bytes `1,,2`. Intended contract: reject an empty numeric field with a handled validation error.
- Reproduction steps supplied by the fictional tester: (1) select a fresh TinyCounts-native0.1 harness with that exact four-byte input; (2) invoke parse once and capture its result/error.
- Supplied transcript: `fatal: field index 2 outside buffer length 2`; harness reports an unhandled parser failure, no parsed result. The same two-step attempt was reported twice.
- Limits: no memory-sanitizer trace, source diagnosis, remote reachability, data read/write or code-execution evidence. These reproduction instructions describe the fictional case; there is no downloadable executable.

**Try:** From the supplied fictional incident, report one supplied observation, one suspected weakness and one unproven impact. Do not claim you reproduced it.
