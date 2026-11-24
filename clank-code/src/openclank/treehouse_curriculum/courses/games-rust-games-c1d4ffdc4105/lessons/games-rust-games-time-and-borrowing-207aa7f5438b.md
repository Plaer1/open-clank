# Test state access and motion

- A shared borrow permits reading; mutable access permits change under Rust's aliasing rules. Bevy additionally checks declared system/query access.
- Two mutable transform queries may overlap even if their variable names differ. Make their entity sets disjoint, or combine the work in one query; do not assume the scheduler can infer your intention.
- Frame-scaled displacement is useful here, but a low frame rate can skip a narrow overlap region. A fixed-step or swept test addresses a different problem from borrowing.
- Reset must restore both position and `won`; resetting only appearance leaves stale game state.

**Self-check:** Change speed to 90 and test the same path. Then explain a reset bug that restores position but leaves `won=true`, and how you would test it.
