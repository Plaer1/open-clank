# Display between known samples

- Remote snapshots describe an earlier authoritative state. Buffering lets the renderer interpolate between known samples despite uneven arrival times.
- Samples (time=100 ms,x=2) and (time=200 ms,x=6) give x=4 at 150 ms under linear interpolation.
- Extrapolation guesses beyond the newest sample; a sudden turn can make that guess wrong. Buffering trades additional display delay for fewer gaps.
- Relevancy sends only useful entities; dormancy avoids repeatedly sending unchanged state. Both need a policy for reappearing or changed objects.

**Self-check:** Find x at 125 ms. Then explain what the renderer could do if the 200 ms sample is missing, including the tradeoff.
