# Name the space before the coordinate

- A mesh contains vertices and primitives; topology says how they connect. Normals describe surface orientation; tangents help interpret directional surface data.
- A local point becomes world-space through an object transform, then view-space through the camera, then clip/screen coordinates through projection and viewport mapping.
- Example in one dimension: local x=2 plus object translation 5 gives world x=7. A camera at world x=3 sees relative x=4 before projection.
- Perspective changes apparent size with depth; orthographic projection preserves parallelism and equal projected size for equal objects at different depths under the same orientation.

**Self-check:** Trace local point (1,2) with object translation (3,0) and camera translation (1,1), ignoring rotation. Explain why adding a world-space light direction to a screen-space pixel position is not meaningful.
