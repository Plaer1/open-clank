# Trace sources and sinks

- A stock accumulates; a flow changes it. For coins: next = current + earned − spent.
- Starting at 10, earning 3 and spending 2 per turn gives 11,12,13 after three turns. Constant positive net flow keeps accumulating in this toy model.
- Reinforcing feedback increases an existing change: more coins buy production that earns more coins. Balancing feedback opposes a change: higher inventory triggers a larger sink.
- A delay can make a correction arrive after the condition has changed. Test the update rule rather than assuming all balancing feedback is stable.

**Self-check:** Trace three turns starting at 10 with earn=3 and spend=floor(current/4), using the old stock for each turn. Name one missing real-game factor.
