# Separate a goal claim from verification

- Native Goals require an eligible UI, a usable model route and the correct supervisor/owner scope. Verify can invoke the agent; it is not a free inspection-only button.
- A goal's trusted evidence and terminal state matter more than an assistant's Done sentence. Refresh stale revisions before a permitted update.

**Offline target:** receipt R1 subtotal 12, tax 1, total 13. Goal G7 revision 4 is awaiting verification, requires command evidence, usedTurns 2/maxTurns 3, and has no attached evidence. The assistant says Done. Revision 5 is verification_degraded: verifier unavailable, still no evidence. An edit against revision 4 is rejected as stale.

**Later supplied display receipt:** G7 revision 6 completed; E1 kind command, producer/source `tool:receipt-check`, verifier `session-tool:receipt-check`, observation `12 + 1 = 13`, evidenceSatisfied=true. This simplified card describes the supplied state; it is not a transport payload or your executed verification.

Wrong owner, missing route, missing supervisor or exhausted budget blocks the corresponding action. None is solved by rewriting Done.
