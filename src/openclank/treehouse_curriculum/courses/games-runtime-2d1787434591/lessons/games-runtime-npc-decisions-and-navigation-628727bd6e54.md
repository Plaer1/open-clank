# Choose a goal before a path

- A state machine changes modes by explicit conditions: Patrol → Chase when a target is seen; Chase → Search when sight is lost.
- A behavior tree organizes conditional actions; a utility system compares action scores; goal-oriented planning searches actions toward a condition. None requires a learned model.
- Perception updates what an NPC knows. Navigation finds a route; steering follows it while avoiding local obstacles. These are different failure points.
- A blackboard is shared decision data, not perfect knowledge. An unseen target should not automatically update its last-seen position.

**Self-check:** Write a three-state NPC table with enter/leave conditions. Diagnose an NPC that chooses a reachable goal but repeatedly bumps into a moving crate.
