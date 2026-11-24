# Simulation time and display time

- A simulation step updates game state; a rendered frame displays a view of it. They need not happen at the same rate.
- At velocity 6 units/second and a fixed step of 0.1 seconds, each step advances 0.6 units. Ten steps advance 6 units.
- A fixed step simplifies repeatable update logic, but does not alone guarantee deterministic physics across machines.
- Interpolating display positions between simulation states can smooth presentation. It must not silently change the authoritative collision state.

**Self-check:** Trace three steps from x=2. Then explain why rendering twice as often should not double the travel distance.
