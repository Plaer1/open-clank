# Keep the editable source

- Raster work stores pixels; vector work stores shapes and paths. Choose based on the edit you need: repainting texture versus changing a scalable contour.
- Keep a layered source document separately from its flattened game texture. Sprite sheets also need frame size, order, pivot and timing metadata.
- A mesh pipeline adds topology, UV mapping, normals and material assignments. A rig adds controls; the runtime may receive baked animation instead of those authoring controls.
- A procedural graph is a recipe; a baked texture or mesh is one output. Retain the recipe, parameters and tool version when reproducibility matters.

**Self-check:** Specify editable source, delivery format and missing metadata for a four-frame sprite and a rigged prop. Explain one edit that the delivery file cannot conveniently recover.
