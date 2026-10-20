# A controller sees a measurement

- A difficulty controller acts on observations: a miss count, completion time or damage taken. Those are imperfect proxies for skill.
- Separate state, observation, decision and action. If a learner pauses to read, elapsed time may rise while their skill is unchanged.
- Update gently and test delayed responses. Changing difficulty every instant can make the system chase its own effects.
- Compare technical efficiency (resources per result), player outcome and resilience. A lean system with no spare capacity may handle disturbances poorly.

**Self-check:** Sketch an assist that observes three attempts before changing an input window. Give a false signal, a limit on adjustment and a way to let the player override it.
