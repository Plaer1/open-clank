# Test the claim the build makes

- A functional test checks behavior; a regression test preserves a previously fixed case. Compatibility, long-session and network-condition tests cover different risks.
- Measure frame time and spikes, memory and load time on the target. 60 frames/second corresponds to about 16.67 ms per frame, but a good average can hide hitches.
- Separate CPU, GPU, I/O and synchronization bottlenecks before optimizing. A technical benchmark does not measure whether players understand a rule.
- Write an acceptance claim with evidence: “restart restores score and position after timeout on this build/platform.” Keep observed scope and remaining gaps.

**Self-check:** Create a four-case test matrix for one tiny game: ordinary success, boundary, restart after failure and one performance spike. Give an expected result for each.
