# Feedback follows game state

- A skeleton supplies joints; skinning deforms a mesh from them. A clip stores change over time; blending combines clips, while inverse kinematics solves a pose toward a target.
- Root motion moves through the animation; a controller can instead own movement. Decide who owns displacement before combining them.
- Trigger sound and animation from a single gameplay event. Route voice, effects and music through separate buses so one preference need not mute everything.
- A spatial sound has an emitter and listener; attenuation changes level with distance. Adaptive music changes with state. Captions and visible cues carry essential meaning without sound.

**Self-check:** Describe one attack event through gameplay, animation and audio. Identify movement ownership and a non-audio cue; explain how rollback would avoid replaying the hit sound twice.
