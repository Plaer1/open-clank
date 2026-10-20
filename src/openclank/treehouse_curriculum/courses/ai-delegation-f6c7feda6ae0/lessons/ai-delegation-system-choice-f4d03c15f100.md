# Compare work, context, money and clock

Use four columns: aggregate work, per-call context, money and elapsed time. The following rates, cache hits, overlap and final results are fictional supplied observations.

Rates per 1,000: fresh input 2/cached input 0.5/output 4 credits. Cached input is included in input. No tool/storage fees. Each call's input plus output must fit a 6,000-token window. Both final results are supplied as 4/4 correct, zero unsupported; the raw cases are absent.

| Plan/event | Input | Cached | Output | Time and dependency |
| --- | ---: | ---: | ---: | --- |
| Single S | 5000 | 0 | 800 | 12 seconds |
| Parallel D0 dispatch | 600 | 500 | 100 | 0–1 |
| WX | 2000 | 500 | 300 | 1–6 |
| WY | 2000 | 500 | 300 | 1–8 |
| SYN | 1400 | 500 | 400 | 8–10, after both workers |

Lead peak input: stable 500 + two summaries of 300 + question 100 + prior 200 = 1,400. Cache still occupies context. Parallel elapsed time is **1 + max(5,7) + 2**; serializing those steps gives 1 + 5 + 7 + 2.

Choose under inclusive budget/deadline pairs 14/11 and 13.3/15.

Continuity cards:
- Fresh definition: “summarize policy”; contains no prior facts.
- Yesterday's session used P1.
- Saved artifact: P2 v2, standard unopened, effective 2026-10-01 onward, sentence 1 says 30 days.
- New task: answer as of 2026-11-03. The artifact is a checkable candidate, not automatic proof of freshness.

Choose mechanisms for independent current documents; tightly coupled debugging; a huge corpus with two relevant sections; and approaching the context limit. Cache, retrieval, session continuity, artifacts and delegation can complement each other. Do not elevate untrusted data into trusted instructions to improve prefix reuse.

Missing worker or stale source leaves final quality pending. Future cache hits, concurrency and latency are unspecified.
