# Carry shape, materials and motion

Optional practice.

- Candidate route: Blender 4.5.14 LTS → GLB → Godot 4.7.2; binary/exporter/import qualification remains open. Work in a new scene.
- Select the cube, rename MarkerBox. Set dimensions(2,1,1)m, location0 and rotation0. Apply Scale so scale reads(1,1,1). Retain its basic UV layer.
- Add simple materials Base(gray) and Mark(red). In Edit Mode, Face Select, select only the local +X face (positive red axis); assign Mark. Other five faces use Base. Leave six quads/12triangles; add no modifiers/armature/textures.
- Set24 fps, frames 1–25. At frame 1 insert a rotation key with Z=0°. At frame 25 set Z=90° and insert rotation key. In animation keyframe interpolation choose Linear; name the action Spin. Save marker-box.blend.
- Select only MarkerBox; export glTF2 binary GLB, selected objects, mesh/materials/UV/animation enabled. Exclude lights/cameras. Save marker-box.glb. Record actual exporter controls; if candidate menus differ, record the mismatch before claiming success.
- Import GLB into a separate Godot Node3D scene. Add Camera3D with Current enabled and position it to see the model; add a light. Inspect the imported hierarchy to locate its animation player rather than assuming a path. Play the imported Spin-equivalent animation.
- Verify ratio2:1:1, two materials, marked-face orientation and a quarter-turn over1 second (24 frame intervals). Blender Z-up converts to glTF/Godot Y-up; raw Euler labels need not match.

**Self-check:** Supply editable source, GLB, versions and actual import observations for dimensions/materials/orientation/animation. Name one information boundary that required checking.
