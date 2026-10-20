# Measure the runtime representation

- Texture resolution, triangle count, skin influences, material changes and animation sampling all affect different costs. Measure the constrained resource before reducing everything.
- Level of detail changes representation with use; instancing can reuse geometry; atlases trade fewer texture bindings against packing and filtering constraints.
- Photogrammetry reconstructs from photographs; neural radiance fields and Gaussian splats represent appearance differently from an ordinary editable mesh. A convincing view does not automatically supply collision, UVs, a rig or a portable game asset.
- Keep generation/capture upstream when possible; inspect output for scale, holes, lighting baked into textures, geometry cost and intended interactions.

**Self-check:** A captured statue looks convincing but blocks a doorway with an oversized collision box. Propose separate visual, collision and performance checks.
