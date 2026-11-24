# A local rule makes a global pattern

- Five cells, indices 0–4, start 10000. All update simultaneously: new[i]=1 if either old neighbor is 1, else 0.
- Ring: neighbors wrap modulo 5. Line: absent neighbors are 0. Optional intervention: on the ring force cell 2 to 0 after every update.

**Self-check:** Compute two updates on the ring and line, then the intervened ring. Explain why in-place updating or a boundary change defines another model.
