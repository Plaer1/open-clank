"""Conservative textured mesh and skinned-result profiles for owned rigging."""
import json
import math
import struct
from src.openclank.blender_child import validate_glb


def inspect_textured_rig_source(data):
    digest = validate_glb(data, textured=True)
    length = struct.unpack_from('<I', data, 12)[0]
    doc = json.loads(data[20:20+length]); offset = 20 + length
    binary = data[offset+8:]
    buffers, views, accessors, meshes, nodes, scenes = (doc.get(k) for k in ('buffers','bufferViews','accessors','meshes','nodes','scenes'))
    if (not isinstance(buffers,list) or len(buffers)!=1 or not isinstance(buffers[0],dict)
            or type(buffers[0].get('byteLength')) is not int or not 0 <= buffers[0]['byteLength'] <= len(binary)
            or not isinstance(views,list) or not isinstance(accessors,list)
            or not isinstance(meshes,list) or not 1 <= len(meshes) <= 64
            or not isinstance(nodes,list) or not 1 <= len(nodes) <= 128
            or not isinstance(scenes,list) or not 1 <= len(scenes) <= 16):
        raise ValueError('unsupported textured mesh')
    binary = binary[:buffers[0]['byteLength']]
    def read(index, kind, components, formats, max_count):
        if type(index) is not int or not 0 <= index < len(accessors): raise ValueError('invalid mesh accessor')
        a=accessors[index]
        if not isinstance(a,dict) or a.get('type')!=kind or a.get('componentType') not in formats or a.get('sparse') is not None or a.get('normalized',False) is not False:
            raise ValueError('unsupported mesh accessor')
        count=a.get('count'); vi=a.get('bufferView')
        if type(count) is not int or not 1<=count<=max_count or type(vi) is not int or not 0<=vi<len(views): raise ValueError('invalid mesh accessor')
        view=views[vi]; fmt=formats[a['componentType']]; width=struct.calcsize(fmt)*components
        if not isinstance(view,dict) or view.get('buffer')!=0 or view.get('byteStride') is not None: raise ValueError('unsupported mesh view')
        start=view.get('byteOffset',0); size=view.get('byteLength'); local=a.get('byteOffset',0)
        if (any(type(v) is not int or v<0 for v in (start,size,local)) or (start+local)%struct.calcsize(fmt)
                or local+count*width>size or start+size>len(binary)): raise ValueError('invalid mesh span')
        return list(struct.iter_unpack('<'+fmt*components,binary[start+local:start+local+count*width]))
    # Count each mesh instance in the actual default scene, not merely each definition.
    scene_id=doc.get('scene',0)
    if type(scene_id) is not int or not 0<=scene_id<len(scenes) or not isinstance(scenes[scene_id],dict): raise ValueError('invalid scene')
    roots=scenes[scene_id].get('nodes')
    if not isinstance(roots,list) or not roots: raise ValueError('invalid scene')
    seen=set(); stack=list(roots); mesh_indices=[]
    while stack:
        n=stack.pop()
        if type(n) is not int or not 0<=n<len(nodes) or n in seen or not isinstance(nodes[n],dict): raise ValueError('invalid scene hierarchy')
        seen.add(n); node=nodes[n]; children=node.get('children',[])
        if not isinstance(children,list): raise ValueError('invalid scene hierarchy')
        stack.extend(children)
        if 'mesh' in node:
            mi=node['mesh']
            if type(mi) is not int or not 0<=mi<len(meshes): raise ValueError('invalid mesh')
            mesh_indices.append(mi)
    if not mesh_indices: raise ValueError('mesh unavailable')
    total=0
    for mi in mesh_indices:
        mesh=meshes[mi]
        if not isinstance(mesh,dict) or not isinstance(mesh.get('primitives'),list) or not 1<=len(mesh['primitives'])<=64: raise ValueError('invalid mesh')
        for primitive in mesh['primitives']:
            if not isinstance(primitive,dict) or primitive.get('mode',4)!=4 or primitive.get('targets') or not isinstance(primitive.get('attributes'),dict): raise ValueError('unsupported primitive')
            attrs=primitive['attributes']
            positions=read(attrs.get('POSITION'),'VEC3',3,{5126:'f'},1000000)
            if any(not math.isfinite(v) for row in positions for v in row): raise ValueError('invalid positions')
            uv=read(attrs.get('TEXCOORD_0'),'VEC2',2,{5126:'f'},1000000)
            if len(uv)!=len(positions) or any(not math.isfinite(v) for row in uv for v in row): raise ValueError('invalid texture coordinates')
            materials=doc.get('materials'); material_index=primitive.get('material')
            if not isinstance(materials,list) or type(material_index) is not int or not 0<=material_index<len(materials): raise ValueError('material unavailable')
            material=materials[material_index]
            if not isinstance(material,dict): raise ValueError('material unavailable')
            pbr=material.get('pbrMetallicRoughness',{})
            if not isinstance(pbr,dict): raise ValueError('textured material unavailable')
            used_textures=[pbr[k] for k in ('baseColorTexture','metallicRoughnessTexture') if k in pbr]
            used_textures += [material[k] for k in ('normalTexture','occlusionTexture','emissiveTexture') if k in material]
            if not used_textures or any(not isinstance(texture,dict) or type(texture.get('texCoord',0)) is not int or texture.get('texCoord',0)!=0 for texture in used_textures):
                raise ValueError('unsupported material texture coordinates')
            if 'indices' in primitive:
                indices=read(primitive['indices'],'SCALAR',1,{5121:'B',5123:'H',5125:'I'},1140000)
                if any(index[0]>=len(positions) for index in indices): raise ValueError('invalid indices')
                count=len(indices)
            else: count=len(positions)
            if count%3: raise ValueError('invalid triangles')
            total+=count//3
            if total>380000: raise ValueError('triangle limit exceeded')
    return {'sha256':digest,'triangles':total}


def inspect_textured_rig_result(data):
    geometry = inspect_textured_rig_source(data)
    from src.openclank.blender_rig_inspect import inspect_textured_rig
    rig = inspect_textured_rig(data)
    if rig['sourceSHA256'] != geometry['sha256']: raise ValueError('rig changed')
    return {**geometry, 'joints':len(rig['hierarchy'])}
