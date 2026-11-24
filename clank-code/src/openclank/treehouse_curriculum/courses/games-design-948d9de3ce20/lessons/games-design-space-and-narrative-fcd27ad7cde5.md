# Space and stateful stories

- Graybox a room with simple shapes before polishing it. Mark an entrance, exit, landmark, optional route and sightline.
- Metrics connect rules to space: at 2 tiles per turn, a 6-tile corridor costs 3 turns if movement is unobstructed.
- A narrative branch is also state: `has_key` can change dialogue and open a door. Store the choice that matters; do not infer it from which line is currently visible.
- Spatial gates, story gates and onboarding cues should communicate their conditions.

**Self-check:** Draw or describe a five-location room graph and one story flag. Give a reachable exit route and the exact condition for an optional branch.
