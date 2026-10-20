# Construct a recognizer from two machines

- Input alphabet is {0,1}; read one symbol at a time. A DFA accepts only if its final state is accepting.
- Machine A recognizes “contains101.” States E(no useful suffix), P(suffix1), Q(suffix10), F(found). Start E; only F accepts.

| A state | on0 | on1 |
|---|---|---|
| E | E | P |
| P | Q | P |
| Q | E | F |
| F | F | F |

- Machine B tracks parity of 1s: start V(even); 0 keeps its state, 1 toggles V↔O. Only V accepts.
- A product state is (A-state,B-state); each symbol updates both components. Accept only when both components accept.

**Try:** Construct all eight product states and their two transitions, start and accepting set. Trace empty input,101,1011,10101. Explain how the construction handles an occurrence overlapping an earlier101 and why it recognizes the intersection.
