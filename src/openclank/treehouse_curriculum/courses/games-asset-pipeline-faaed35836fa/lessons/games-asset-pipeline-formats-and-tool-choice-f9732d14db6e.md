# Choose a format for the information

- glTF/GLB targets runtime scene interchange; native DCC files retain tool-specific editing structures. Godot's direct Blender import invokes a glTF conversion, so it still has an interchange boundary.
- FBX, OBJ, glTF, USD and Alembic do not carry identical information. USD adds scene composition/layers; Alembic is used for baked geometric animation; MaterialX describes material networks. Test the actual exporter/importer pair.
- PNG can deliver ordinary textures; EXR can carry high-range image data. Neither promises to preserve every layer or procedural node of an authoring document.
- Select tools by the operation: painting/compositing, vector design, modeling/sculpting, rigging, material authoring, procedural construction. A familiar tool plus a tested interchange path can be more useful than a larger untested stack.

**Self-check:** For a static prop, animated character and layered multi-artist scene, specify the information each handoff must retain. Choose one candidate format per task and name a feature you would test before adopting it.
