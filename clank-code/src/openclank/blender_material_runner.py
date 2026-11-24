"""Fixed isolated Blender 4.5 CPU material-channel bake child."""
import math
import sys
from pathlib import Path

import bpy

args = sys.argv[sys.argv.index('--') + 1:]
if len(args) != 3 or args[2] not in ('256', '512', '1024', '2048'):
    raise SystemExit(2)
source, root, resolution = Path(args[0]), Path(args[1]), int(args[2])
if bpy.app.version[:2] != (4, 5): raise ValueError('Blender 4.5 required')
bpy.ops.wm.read_factory_settings(use_empty=True)
if bpy.ops.import_scene.gltf(filepath=str(source)) != {'FINISHED'}: raise ValueError('GLB import unavailable')
scene = bpy.context.scene
objects = list(scene.objects)
if len(objects) != 1 or objects[0].type != 'MESH': raise ValueError('material scene unavailable')
obj = objects[0]
if (obj.modifiers or obj.constraints or obj.animation_data or obj.data.animation_data or obj.data.shape_keys
        or len(obj.data.materials) != 1 or len(obj.data.uv_layers) != 1 or not obj.data.uv_layers[0].data
        or not obj.data.vertices or not obj.data.polygons or any(poly.material_index != 0 for poly in obj.data.polygons)):
    raise ValueError('static material mesh unavailable')
if (len(obj.data.vertices) > 1000000 or len(obj.data.loops) > 4000000
        or any(not all(math.isfinite(x) and abs(x) <= 1e7 for x in (obj.matrix_world @ vertex.co)) for vertex in obj.data.vertices)
        or any(not all(math.isfinite(x) and 0 <= x <= 1 for x in uv.uv) for uv in obj.data.uv_layers[0].data)):
    raise ValueError('material geometry unavailable')
material = obj.data.materials[0]
if not material or not material.use_nodes or not material.node_tree or material.node_tree.animation_data:
    raise ValueError('material nodes unavailable')
nodes, links = material.node_tree.nodes, material.node_tree.links
principled = [node for node in nodes if node.type == 'BSDF_PRINCIPLED']
outputs = [node for node in nodes if node.type == 'OUTPUT_MATERIAL' and node.is_active_output]
if len(principled) != 1 or len(outputs) != 1: raise ValueError('material output unavailable')
shader, output = principled[0], outputs[0]
surface = output.inputs.get('Surface')
if not surface or len(surface.links) != 1 or surface.links[0].from_node != shader or surface.links[0].from_socket != shader.outputs.get('BSDF'):
    raise ValueError('material output unavailable')
channels = [(shader.inputs.get('Base Color'), 'base-color.png', 'sRGB'),
            (shader.inputs.get('Roughness'), 'roughness.png', 'Non-Color'),
            (shader.inputs.get('Metallic'), 'metallic.png', 'Non-Color')]
if any(socket is None or len(socket.links) > 1 for socket, _, _ in channels): raise ValueError('material channel unavailable')
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
scene.cycles.samples = 1
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
emission = nodes.new('ShaderNodeEmission')
emission.inputs['Strength'].default_value = 1.0
links.remove(surface.links[0])
links.new(emission.outputs['Emission'], surface)
for socket, filename, colorspace in channels:
    image = bpy.data.images.new('openclank-bake-' + filename, width=resolution, height=resolution, alpha=False, float_buffer=False)
    image.generated_color = (0.0, 0.0, 0.0, 1.0)
    image.colorspace_settings.name = colorspace
    target = nodes.new('ShaderNodeTexImage')
    target.image = image
    nodes.active = target
    color = emission.inputs['Color']
    for link in list(color.links): links.remove(link)
    if socket.is_linked:
        links.new(socket.links[0].from_socket, color)
    elif socket.type == 'RGBA':
        color.default_value = socket.default_value
    elif socket.type == 'VALUE':
        value = float(socket.default_value)
        if not math.isfinite(value) or not 0 <= value <= 1: raise ValueError('material factor unavailable')
        color.default_value = (value, value, value, 1.0)
    else:
        raise ValueError('material channel unavailable')
    result = bpy.ops.object.bake(type='EMIT', target='IMAGE_TEXTURES', save_mode='INTERNAL', use_selected_to_active=False,
                                 use_clear=False, margin=0, uv_layer=obj.data.uv_layers[0].name)
    if result != {'FINISHED'}: raise ValueError('material bake unavailable')
    image.file_format = 'PNG'
    image.filepath_raw = str(root / filename)
    image.save()
    nodes.remove(target)
