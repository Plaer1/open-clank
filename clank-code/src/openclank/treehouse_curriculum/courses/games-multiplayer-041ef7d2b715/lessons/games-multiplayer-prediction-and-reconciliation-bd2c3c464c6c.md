# Correct the past; replay the pending inputs

- Predict local input immediately for responsive control. Keep numbered inputs until the authority acknowledges them.
- Correction says “state x=4 after input 10.” Restore that state, discard acknowledged inputs and replay later inputs once.
- Example pending inputs 11:+2, 12:−1 yield x=5. Replaying input 10 again would double-count it.
- Render smoothing can hide a visual snap, but it must not replace the corrected gameplay state.

**Self-check:** Starting with authoritative x=7 after input 20, replay 21:−2, 22:+3 and 23:+1. State which inputs remain queued and why.
