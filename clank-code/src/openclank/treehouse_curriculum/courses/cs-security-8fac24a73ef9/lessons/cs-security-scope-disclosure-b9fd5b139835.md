# Scope and factual reporting

- Specify the owned target, permitted actions, data, limits, and stop condition before an experiment.
- Record minimal reproduction, actual result, expected result, affected version, and conservative impact. Keep secrets and unrelated data out.
- Use the responsible party's stated reporting process when appropriate; this exercise drafts a report and sends nothing.

**Fictional incident card — parser-crash-v1.** All observations below are supplied teaching data, not a run you performed.

- Owned target: TinyCounts-native0.1 in an isolated local test harness; no network/service or real user data.
- Input: ASCII bytes `1,,2`. Intended contract: reject an empty numeric field with a handled validation error.
- Reproduction steps supplied by the fictional tester: (1) select a fresh TinyCounts-native0.1 harness with that exact four-byte input; (2) invoke parse once and capture its result/error.
- Supplied transcript: `fatal: field index 2 outside buffer length 2`; harness reports an unhandled parser failure, no parsed result. The same two-step attempt was reported twice.
- Limits: no memory-sanitizer trace, source diagnosis, remote reachability, data read/write or code-execution evidence. These reproduction instructions describe the fictional case; there is no downloadable executable.

**Try:** Draft a report from this fictional incident: scope/version, the two supplied steps, expected versus supplied result, unknown impact and a proposed next check. Attribute the evidence to the card; do not claim your own run.
