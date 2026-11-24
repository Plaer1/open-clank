# Finite-state machines

- A finite automaton has states, input symbols, transitions, a start, and accepting states.
- To recognize binary strings ending in 1, remember only whether the latest symbol is 1. Start nonaccepting; 0 goes nonaccepting, 1 accepting.
- A regular expression describes a regular language; a finite automaton can recognize it.

**Try:** Trace 1010 and 001. Then explain what extra history a recognizer for 'contains 101' must retain.
