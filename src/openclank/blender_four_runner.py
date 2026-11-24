"""Fixed four-view CPU render; only an owned input and output directory."""
import math
import sys
from pathlib import Path
import bpy
from mathutils import Vector

args=sys.argv[sys.argv.index('--')+1:]
if len(args)!=2: raise SystemExit(2)
source,root=map(Path,args)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(source))
meshes=[obj for obj in bpy.context.scene.objects if obj.type=='MESH']
if not meshes: raise ValueError('GLB has no mesh')
points=[obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
lo=Vector(tuple(min(p[i] for p in points) for i in range(3)))
hi=Vector(tuple(max(p[i] for p in points) for i in range(3)))
center=(lo+hi)/2; size=max(hi-lo)
if not math.isfinite(size) or size<=0 or size>1e7: raise ValueError('unsupported mesh bounds')
scene=bpy.context.scene; scene.render.engine='CYCLES'; scene.cycles.device='CPU'; scene.cycles.samples=16
scene.world.color=(0.15,0.15,0.15)
light_data=bpy.data.lights.new('light','AREA'); light=bpy.data.objects.new('light',light_data)
scene.collection.objects.link(light); light.location=center+Vector((size,-2*size,2*size))
light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
light_data.energy=max(1.0,size*size*500.0); light_data.size=size*2
scene.render.resolution_x=scene.render.resolution_y=512; scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG'
camera_data=bpy.data.cameras.new('camera'); camera=bpy.data.objects.new('camera',camera_data)
scene.collection.objects.link(camera); camera_data.type='ORTHO'; camera_data.ortho_scale=size*1.25; scene.camera=camera
for index,offset in enumerate(((0,-3,0),(3,0,0),(0,3,0),(-3,0,0))):
    camera.location=center+Vector(offset)*size
    camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
    scene.render.filepath=str(root / ('view%d.png' % index))
    bpy.ops.render.render(write_still=True)
