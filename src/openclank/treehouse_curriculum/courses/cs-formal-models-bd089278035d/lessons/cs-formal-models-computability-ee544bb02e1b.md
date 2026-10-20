# What no general algorithm can decide

- A Turing machine models computation with finite control and an unbounded tape; it is a mathematical model, not a claim about physical memory.
- Suppose H(P,x) always decides whether P halts on x. Define D(P): loop if H(P,P) says halt; otherwise halt.
- D(D) contradicts either prediction. Therefore a universally correct halting decider cannot exist.

**Try:** Explain both cases of D(D), then say why this does not prevent deciding a particular simple loop.
