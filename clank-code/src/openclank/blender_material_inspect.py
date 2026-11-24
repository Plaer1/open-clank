"""Bounded metadata gate for the fixed Blender material-channel recipe.

This is deliberately not a complete glTF decoder. The fixed child independently
checks the imported scene and actual UVs before baking.
"""
import json
import math
import struct

from src.openclank.blender_child import validate_glb


def inspect_material_source(raw: bytes) -> dict[str, str]:
    digest = validate_glb(raw, textured=True)
    length = struct.unpack_from('<I', raw, 12)[0]
    doc = json.loads(raw[20:20 + length].decode('utf-8'))
    if doc.get('animations') or doc.get('skins'):
        raise ValueError('animated or skinned material source unavailable')
    scenes, nodes, meshes, materials = (doc.get(key) for key in ('scenes', 'nodes', 'meshes', 'materials'))
    if (not isinstance(scenes, list) or len(scenes) != 1 or not isinstance(nodes, list) or len(nodes) != 1
            or not isinstance(meshes, list) or len(meshes) != 1 or not isinstance(materials, list) or len(materials) != 1
            or type(doc.get('scene', 0)) is not int or doc.get('scene', 0) != 0 or not isinstance(scenes[0], dict)
            or type(scenes[0].get('nodes')) is not list or len(scenes[0]['nodes']) != 1
            or type(scenes[0]['nodes'][0]) is not int or scenes[0]['nodes'][0] != 0):
        raise ValueError('material scene unavailable')
    node, mesh, material = nodes[0], meshes[0], materials[0]
    if (not isinstance(node, dict) or type(node.get('mesh')) is not int or node.get('mesh') != 0 or any(key in node for key in ('skin', 'weights', 'children'))
            or not isinstance(mesh, dict) or 'weights' in mesh or not isinstance(mesh.get('primitives'), list)
            or len(mesh['primitives']) != 1 or not isinstance(material, dict) or material.get('alphaMode', 'OPAQUE') != 'OPAQUE'):
        raise ValueError('material mesh unavailable')
    primitive = mesh['primitives'][0]
    if (not isinstance(primitive, dict) or primitive.get('mode', 4) != 4 or type(primitive.get('material')) is not int or primitive.get('material') != 0
            or 'targets' in primitive or not isinstance(primitive.get('attributes'), dict)):
        raise ValueError('material primitive unavailable')
    attrs = primitive['attributes']
    if 'POSITION' not in attrs or 'TEXCOORD_0' not in attrs or any(key.startswith(('JOINTS_', 'WEIGHTS_')) for key in attrs):
        raise ValueError('material UV unavailable')
    accessors, views = doc.get('accessors'), doc.get('bufferViews')
    if not isinstance(accessors, list) or not isinstance(views, list):
        raise ValueError('material accessors unavailable')
    def accessor(index, kind, components):
        if type(index) is not int or not 0 <= index < len(accessors): raise ValueError('accessor unavailable')
        item = accessors[index]
        if (not isinstance(item, dict) or item.get('type') != kind or item.get('componentType') not in components
                or 'sparse' in item or type(item.get('count')) is not int or item['count'] < 1
                or type(item.get('bufferView')) is not int or not 0 <= item['bufferView'] < len(views)):
            raise ValueError('accessor unavailable')
        view = views[item['bufferView']]
        sizes = {5121:1, 5123:2, 5125:4, 5126:4}
        width = {'SCALAR':1, 'VEC2':2, 'VEC3':3}[kind] * sizes[item['componentType']]
        offset, stride = item.get('byteOffset', 0), view.get('byteStride', width)
        if (type(offset) is not int or offset < 0 or type(stride) is not int or stride < width
                or type(view.get('byteLength')) is not int or offset + (item['count'] - 1) * stride + width > view['byteLength']):
            raise ValueError('accessor range unavailable')
        return item['count']
    count = accessor(attrs['POSITION'], 'VEC3', (5126,))
    if accessor(attrs['TEXCOORD_0'], 'VEC2', (5126, 5121, 5123)) != count:
        raise ValueError('UV count unavailable')
    if 'indices' in primitive: accessor(primitive['indices'], 'SCALAR', (5121, 5123, 5125))
    uv = accessors[attrs['TEXCOORD_0']]
    if uv['componentType'] in (5121, 5123) and uv.get('normalized') is not True:
        raise ValueError('material UV normalization unavailable')
    pbr = material.get('pbrMetallicRoughness', {})
    if not isinstance(pbr, dict): raise ValueError('material PBR unavailable')
    factors = pbr.get('baseColorFactor', [1, 1, 1, 1])
    if (not isinstance(factors, list) or len(factors) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in factors)
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                   for value in (pbr.get('roughnessFactor', 1), pbr.get('metallicFactor', 1)))):
        raise ValueError('material factors unavailable')
    textures = doc.get('textures')
    selected = [pbr.get('baseColorTexture'), pbr.get('metallicRoughnessTexture')]
    if not any(value is not None for value in selected) or not isinstance(textures, list):
        raise ValueError('material texture unavailable')
    images = doc.get('images', [])
    for texture in selected:
        if texture is None: continue
        if (not isinstance(texture, dict) or type(texture.get('texCoord', 0)) is not int or texture.get('texCoord', 0) != 0 or type(texture.get('index')) is not int
                or not 0 <= texture['index'] < len(textures)):
            raise ValueError('material texture unavailable')
        entry = textures[texture['index']]
        if not isinstance(entry, dict) or type(entry.get('source')) is not int or not 0 <= entry['source'] < len(images):
            raise ValueError('material image unavailable')
    return {'source_sha256': digest}
