# Retrieve, inspect and cite

- Retrieval selects external information to supply in model context. Storing or indexing a record does not automatically inject it, and none of these steps trains the weights.
- Trace documents → metadata/index → candidates → date/scope selection → selected passage in context → grounded answer.

Fictional query: **As of 2026-11-03, how long can a standard customer return an unopened item?**

| Record | Scope and validity | Supplied text |
| --- | --- | --- |
| P1 v1 | standard unopened; 2026-01-01 through 2026-09-30 | Sentence 1: “Standard unopened items may be returned within 14 days.” Sentence 2: “This record ends on 2026-09-30.” |
| P2 v2 | standard unopened; 2026-10-01 onward | Sentence 1: “Standard unopened items may be returned within 30 days.” Sentence 2: “Opened items require separate review.” |
| P3 v1 | business-account unopened; 2026-10-01 onward | Sentence 1: “Business-account unopened items may be returned within 60 days.” Sentence 2: “This policy applies to business accounts only.” |

Index I1 contains all three records. Supplied similarity scores: P1 0.94, P3 0.91, P2 0.87. These are toy ranking values, not measured embeddings or probabilities.

P2 sentence 1 supports 30 days for this date and scope. P1 is expired; P3 applies to business accounts. The highest score is not the applicable policy. The supplied records do not establish an opened-item window.

An unavailable index means retrieval did not occur. An available index with no eligible match means no match in that indexed set, not proof that no policy exists.
