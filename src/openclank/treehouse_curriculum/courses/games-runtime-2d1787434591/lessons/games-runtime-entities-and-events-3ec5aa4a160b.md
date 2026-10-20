# Store state; send events

- An entity is an identity in the game. Components describe aspects such as position or health; systems operate on selected aspects. A scene tree also expresses hierarchy and lifetime.
- State answers “what is true now?” An event answers “what happened?” Keep `door_open=true` as state; emit `door_opened` once for sound or animation.
- Choose a single owner for a state change. Two listeners each subtracting damage can apply one hit twice.
- Save durable state with a version; rebuild temporary effects on load. Saving a pointer or current sound playback is not a substitute for saving game meaning.

**Self-check:** Give an entity/component sketch for a door and a switch. Describe what must be saved so loading does not charge the key twice.
