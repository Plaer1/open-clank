# Separate an offer from a subscription

- The model owner shares an eligible model row with chosen recipients. A recipient adds it through Settings → Add Models → Shared with you.
- An active offer, recipient opt-in and successful request resolution are separate states.

**Offline sequence:** Ada owns endpoint E7 and model M-small. Offer S7 names Bea, not Cal. T0: no offer. T1: offer active, Bea opted out. T2: Bea chooses Add to my models; route S7 uses Ada's credential. T3: Bea opts out; Ada's offer remains active. T4: Ada removes Bea; Bea's old tab still shows S7 but request resolution returns not found.

At T4 refresh or choose a permitted route. Do not silently substitute a model or infer free use, all-model access or file access.
