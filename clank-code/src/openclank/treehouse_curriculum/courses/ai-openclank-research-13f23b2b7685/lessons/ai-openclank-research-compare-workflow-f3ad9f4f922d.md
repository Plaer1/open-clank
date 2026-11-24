# Compare answers before choosing a winner

- Fix the prompt and criteria before judging. Reveal identifies models; Vote records a preference. Neither is a correctness check by itself.

**Prompt:** `Three boxes contain 4 pencils each. Two pencils are given away. Return pencils left, then one equation. ≤12 words.`

**Completed cards:** A, 900 ms: `10 pencils. 3 × 4 − 2 = 10.` B, 600 ms: `14 pencils. 3 × 4 + 2 = 14.` Reveal: A=Cedar, B=Juniper. Judge correctness, equation and length: A wins despite being slower. Alternate B=`10 pencils. 12 − 2 = 10.` supports a tie on these criteria.

**Interrupted cards:** A partial=`10 pencils`; B=Timeout. Stop all leaves insufficient completed evidence for this comparison. Sequential B starts after A; that is not a simultaneous speed race. Reroll starts a new call from the first prompt; stopping does not promise zero cost.

**Retention trace:** Save off; vote A; browser-local vote write succeeds; backend record write fails due to network; closing successfully deletes the comparison panes. The local vote remains in browser-wide storage `odysseus-compare-votes`; it is not proven account-isolated, cross-device or saved on the backend. Cleanup is an attempted operation in general, not a guarantee.
