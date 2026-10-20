# Test one asset across the boundary

- UVs map surface points to texture coordinates; seams and distortion are choices to inspect. Normals and tangents affect shading independently of the silhouette.
- Baking transfers a chosen property into a target representation, such as high-detail normal information onto a lower-detail mesh's UV map.
- Use a deliberately simple test asset: an asymmetric box with one marked face, a known size, one material and a short rotation animation. Asymmetry exposes flipped axes and orientation mistakes.
- An import check compares scale, orientation, UVs, material interpretation and animation. It does not rely on matching one flattering screenshot.

**Self-check:** Write an import test for the box, including expected dimensions and a check for each named property. Explain what a normal map cannot fix.
