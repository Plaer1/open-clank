# Reuse one local filter

- Toy rule: dot product with width-two filter [1,1], stride 1, no padding.
- Row [0,1,0,1,1] → [1,1,1,2]. Adjacent ones produce 2.
- Changed row: prepend zero, giving [0,0,1,0,1,1]. Apply the same filter; do not learn a new one.
This illustrates local receptive fields and weight sharing, not recognition accuracy or guaranteed translation invariance of every network.
