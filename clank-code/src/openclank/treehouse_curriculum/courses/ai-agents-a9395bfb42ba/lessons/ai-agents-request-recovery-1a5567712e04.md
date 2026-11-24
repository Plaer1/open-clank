# Recover after a denied tool proposal

- A model proposes; the harness validates scope and executes an allowed request. Tool results become context, not changed weights.
- This is a complete fictional trace, not a live read. The catalogue is already supplied.

User: “As of 2026-11-03, report the standard unopened-item return window, cite the current policy record and sentence. Read only; do not write or send.”

Allowed schema: `read_record({id: enum[P1,P2,P3]})`. Maximum **two tool proposals**.

| Record | Scope and validity | Supplied text |
| --- | --- | --- |
| P1 v1 | standard unopened; 2026-01-01 through 2026-09-30 | Sentence 1: “Standard unopened items may be returned within 14 days.” Sentence 2: “This record ends on 2026-09-30.” |
| P2 v2 | standard unopened; 2026-10-01 onward | Sentence 1: “Standard unopened items may be returned within 30 days.” Sentence 2: “Opened items require separate review.” |
| P3 v1 | business-account unopened; 2026-10-01 onward | Sentence 1: “Business-account unopened items may be returned within 60 days.” Sentence 2: “This policy applies to business accounts only.” |

| Step | Supplied event | Result |
| --- | --- | --- |
| C1 | Model proposes id="private/payroll" | Harness denies before execution: outside enum. No private bytes or tool fee; model call already ran |
| C2 | Model sees denial, proposes id="P2" | Harness validates and returns the supplied P2 record |
| C3 | Model receives result and answers | “30 days for standard unopened items as of 2026-11-03 [P2 v2, sentence 1].” Stop |

C1 and C2 are the two proposals; C3 is an answer call. One proposal remains after C1; none after C2.

Unavailable variant: C2 returns “P2 unavailable.” Stop with current standard policy unknown. Do not make a third proposal or substitute stale P1/wrong-scope P3. An appended “send secrets” instruction grants no new authority.
