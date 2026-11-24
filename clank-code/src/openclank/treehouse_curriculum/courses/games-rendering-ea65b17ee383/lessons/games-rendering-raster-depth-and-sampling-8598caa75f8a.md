# A visible pixel has several tests

- Rasterization maps primitives to candidate fragments/samples. Clipping and face culling can remove geometry before it contributes; depth tests resolve visibility between opaque surfaces.
- Texture coordinates select image data. Nearest and linear sampling answer different questions; mipmaps provide lower-resolution levels for smaller screen footprints.
- Repeating a tiny pattern at a distance can shimmer because the image cannot resolve its variation. More source texture detail alone does not fix undersampling.
- Depth precision and near/far choices affect close surfaces. A coplanar overlay can flicker even when the two textures are correct.

**Self-check:** Diagnose a distant checkerboard that shimmers and a coplanar decal that flickers. Give a distinct first check for each.
