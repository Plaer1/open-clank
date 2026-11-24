# Replay without duplicating effects

- Lockstep waits for the inputs needed for a tick. Rollback predicts missing input, then restores and re-simulates when a prediction differs.
- Replaying requires the relevant saved state: positions, timers, random-generator state and other simulation inputs. Fixed steps alone do not guarantee matching results.
- Server rewind for a shot asks which historical view should be judged. It is a fairness policy, distinct from replaying a local simulation.
- Presentation needs event handling too: re-simulating a tick should not play the same sound or award the same external purchase again.

**Self-check:** Start x=0. Predicted inputs for ticks 1–3 are +1,+1,+1; actual tick 2 is −1. Restore the state after tick 1 and replay through tick 3. Name one state item and one side effect requiring special handling.
