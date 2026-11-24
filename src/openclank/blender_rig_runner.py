"""Fixed imported-bone-local X pose clip, no model-authored script."""
import hashlib, json, math, sys
from pathlib import Path
import bpy
from mathutils import Quaternion, Vector

def validate_mapping(rig, observed):
    hierarchy=rig['hierarchy']; by_index={v['nodeIndex']:v['jointName'] for v in hierarchy}
    expected={v['jointName']:(by_index[v['parentNode']] if v['parentNode'] is not None else None) for v in hierarchy}
    if observed!=expected or rig['selected'] not in hierarchy: raise ValueError('rig mapping changed')
    return rig['selected']['jointName']

args=sys.argv[sys.argv.index('--')+1:]
if len(args)!=3: raise SystemExit(2)
source,root,descriptor=map(Path,args)
rig=json.loads(descriptor.read_text(encoding='utf-8'))
bpy.ops.wm.read_factory_settings(use_empty=True)
operator=bpy.types.Operator.bl_rna_get_subclass_py('IMPORT_SCENE_OT_gltf')
if operator is None: raise ValueError('glTF importer unavailable')
module=sys.modules.get(operator.__module__)
if module is None: raise ValueError('glTF importer identity unavailable')
source_sha=None
try:
    p=Path(module.__file__); raw=p.read_bytes()
    if len(raw)<=2*1024*1024: source_sha=hashlib.sha256(raw).hexdigest()
except (OSError,TypeError,AttributeError): pass
importer={'blenderVersion':bpy.app.version_string,'operator':'IMPORT_SCENE_OT_gltf',
          'module':operator.__module__,'moduleVersion':str(getattr(module,'__version__','unknown'))[:128],
          'sourceFileSHA256':source_sha,'settings':'factory-default-gltf-v1'}
if bpy.ops.import_scene.gltf(filepath=str(source)) != {'FINISHED'}: raise ValueError('glTF import failed')
armatures=[o for o in bpy.context.scene.objects if o.type=='ARMATURE']
meshes=[o for o in bpy.context.scene.objects if o.type=='MESH']
if len(armatures)!=1 or len(meshes)!=1: raise ValueError('rig mapping unavailable')
armature=armatures[0]; mesh=meshes[0]
mods=[m for m in mesh.modifiers if m.type=='ARMATURE']
if len(mods)!=1 or mods[0].object!=armature: raise ValueError('rig association unavailable')
observed={bone.name:(bone.parent.name if bone.parent else None) for bone in armature.pose.bones}
bone=armature.pose.bones.get(validate_mapping(rig,observed))
if bone is None: raise ValueError('rig selection changed')
group=mesh.vertex_groups.get(bone.name)
if group is None or not any(item.group==group.index and item.weight>0 for vertex in mesh.data.vertices for item in vertex.groups):
    raise ValueError('selected joint influence unavailable')
points=[mesh.matrix_world @ Vector(corner) for corner in mesh.bound_box]
lo=Vector(tuple(min(p[i] for p in points) for i in range(3))); hi=Vector(tuple(max(p[i] for p in points) for i in range(3)))
center=(lo+hi)/2; size=max(hi-lo)
if not math.isfinite(size) or size<=0 or size>1e7: raise ValueError('unsupported mesh bounds')
scene=bpy.context.scene; scene.render.engine='CYCLES'; scene.cycles.device='CPU'; scene.cycles.samples=16; scene.world.color=(0.15,0.15,0.15)
light_data=bpy.data.lights.new('light','AREA'); light=bpy.data.objects.new('light',light_data); scene.collection.objects.link(light)
light.location=center+Vector((size,-2*size,2*size)); light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler(); light_data.energy=max(1.0,size*size*500); light_data.size=size*2
scene.render.resolution_x=scene.render.resolution_y=512; scene.render.resolution_percentage=100; scene.render.image_settings.file_format='PNG'
camera_data=bpy.data.cameras.new('camera'); camera=bpy.data.objects.new('camera',camera_data); scene.collection.objects.link(camera)
camera.location=center+Vector((0,-3*size,0)); camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler(); camera_data.type='ORTHO'; camera_data.ortho_scale=size*1.25; scene.camera=camera
base=bone.matrix_basis.copy()
try:
    for index,degrees in enumerate((0,15,0,-15)):
        bone.matrix_basis=base @ Quaternion((1,0,0),math.radians(degrees)).to_matrix().to_4x4()
        bpy.context.view_layer.update(); scene.render.filepath=str(root/('view%d.png'%index)); bpy.ops.render.render(write_still=True)
finally:
    bone.matrix_basis=base
(root/'importer.json').write_text(json.dumps(importer,separators=(',',':')),encoding='utf-8')
