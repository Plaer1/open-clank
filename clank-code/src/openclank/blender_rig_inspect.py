"""Bounded, deliberately narrow self-contained glTF skin inspection."""
import json, math, re, struct
from src.openclank.blender_child import validate_glb

_NAME=re.compile(r'[A-Za-z][A-Za-z0-9_]{0,47}\Z')

def validate_rig_witness(rig, source_sha256):
    if not isinstance(rig,dict) or set(rig)!={'sourceSHA256','skinIndex','hierarchy','selected'} or rig['sourceSHA256']!=source_sha256 or type(rig['skinIndex']) is not int or rig['skinIndex']!=0:
        raise ValueError('invalid rig witness')
    rows=rig['hierarchy']; selected=rig['selected']; keys={'nodeIndex','jointOrdinal','jointName','parentNode'}
    if (not isinstance(rows,list) or not 2<=len(rows)<=64 or not isinstance(selected,dict) or set(selected)!=keys
            or type(selected['nodeIndex']) is not int or type(selected['jointOrdinal']) is not int
            or (selected['parentNode'] is not None and type(selected['parentNode']) is not int) or selected not in rows): raise ValueError('invalid rig witness')
    indices=set(); names=set()
    for ordinal,row in enumerate(rows):
        if (not isinstance(row,dict) or set(row)!=keys or type(row['nodeIndex']) is not int or not 0<=row['nodeIndex']<128
                or type(row['jointOrdinal']) is not int or row['jointOrdinal']!=ordinal or not isinstance(row['jointName'],str)
                or not _NAME.fullmatch(row['jointName']) or row['jointName'] in names or row['nodeIndex'] in indices
                or (row['parentNode'] is not None and (type(row['parentNode']) is not int or not 0<=row['parentNode']<128))): raise ValueError('invalid rig witness')
        indices.add(row['nodeIndex']); names.add(row['jointName'])
    if sum(row['parentNode'] is None for row in rows)!=1 or any(row['parentNode'] is not None and row['parentNode'] not in indices for row in rows): raise ValueError('invalid rig witness')
    parents={row['nodeIndex']:row['parentNode'] for row in rows}
    for node in indices:
        seen=set()
        while node is not None:
            if node in seen: raise ValueError('invalid rig witness')
            seen.add(node); node=parents[node]
    return json.loads(json.dumps(rig))

def inspect_rig(data):
    return _inspect_rig(data, textured=False)

def inspect_textured_rig(data):
    """Distinct textured-skin profile; never creates the legacy clip witness."""
    return _inspect_rig(data, textured=True)

def _inspect_rig(data, *, textured):
    digest=validate_glb(data, textured=textured)
    length=struct.unpack_from('<I',data,12)[0]; doc=json.loads(data[20:20+length]); offset=20+length
    if offset+8>len(data): raise ValueError('rig binary unavailable')
    binary_len=struct.unpack_from('<I',data,offset)[0]; binary=data[offset+8:]
    if len(binary)!=binary_len: raise ValueError('rig binary changed')
    if doc.get('animations') or (doc.get('images') and not textured) or doc.get('extensionsUsed') or doc.get('extensionsRequired'): raise ValueError('unsupported rig features')
    pending=[doc]
    while pending:
        value=pending.pop()
        if isinstance(value,dict):
            if 'extensions' in value: raise ValueError('rig extensions unsupported')
            pending.extend(value.values())
        elif isinstance(value,list): pending.extend(value)
    nodes=doc.get('nodes'); skins=doc.get('skins'); meshes=doc.get('meshes'); scenes=doc.get('scenes')
    if not isinstance(nodes,list) or len(nodes)>128 or not isinstance(skins,list) or len(skins)!=1 or not isinstance(meshes,list) or len(meshes)!=1 or not isinstance(scenes,list) or len(scenes)!=1: raise ValueError('unsupported rig structure')
    skin=skins[0]; joints=skin.get('joints') if isinstance(skin,dict) else None
    if not isinstance(joints,list) or not 2<=len(joints)<=64 or any(type(j) is not int or not 0<=j<len(nodes) for j in joints) or len(set(joints))!=len(joints): raise ValueError('unsupported joints')
    if any(not isinstance(n,dict) for n in nodes): raise ValueError('invalid nodes')
    parents={}
    for i,node in enumerate(nodes):
        children=node.get('children',[])
        if not isinstance(children,list) or any(type(j) is not int or not 0<=j<len(nodes) for j in children): raise ValueError('invalid hierarchy')
        for j in children:
            if j in parents: raise ValueError('ambiguous hierarchy')
            parents[j]=i
        for field,amount in (('matrix',16),('translation',3),('rotation',4),('scale',3)):
            if field in node and (not isinstance(node[field],list) or len(node[field])!=amount or any(type(v) not in (int,float) or not math.isfinite(v) for v in node[field])): raise ValueError('invalid transform')
        if 'matrix' in node and any(k in node for k in ('translation','rotation','scale')): raise ValueError('ambiguous transform')
    for i in range(len(nodes)):
        seen=set(); cursor=i
        while cursor in parents:
            if cursor in seen: raise ValueError('cyclic hierarchy')
            seen.add(cursor); cursor=parents[cursor]
    scene=scenes[0]; roots=scene.get('nodes') if isinstance(scene,dict) else None
    if not isinstance(roots,list) or any(type(i) is not int or not 0<=i<len(nodes) or i in parents for i in roots) or len(set(roots))!=len(roots): raise ValueError('invalid scene roots')
    reachable=set(); pending=list(roots)
    while pending:
        node=pending.pop()
        if node in reachable: raise ValueError('ambiguous scene')
        reachable.add(node); pending.extend(nodes[node].get('children',[]))
    if reachable!=set(range(len(nodes))): raise ValueError('unsupported detached nodes')
    names=[]; rows=[]
    for ordinal,index in enumerate(joints):
        name=nodes[index].get('name'); parent=parents.get(index)
        if (not isinstance(name,str) or not name or name in names or
                (not textured and not _NAME.fullmatch(name)) or
                (textured and (len(name.encode('utf-8','strict'))>128 or any(ord(c)<32 or ord(c)==127 for c in name)))): raise ValueError('joint name unavailable')
        if parent is not None and parent not in joints: parent=None
        names.append(name); rows.append({'nodeIndex':index,'jointOrdinal':ordinal,'jointName':name,'parentNode':parent})
    if sum(row['parentNode'] is None for row in rows)!=1: raise ValueError('unsupported joint hierarchy')
    root=next(row['nodeIndex'] for row in rows if row['parentNode'] is None)
    if 'skeleton' in skin and (type(skin['skeleton']) is not int or skin['skeleton']!=root): raise ValueError('unsupported skeleton root')
    mesh_nodes=[(i,n) for i,n in enumerate(nodes) if 'mesh' in n or 'skin' in n]
    if (len(mesh_nodes)!=1 or type(mesh_nodes[0][1].get('mesh')) is not int or mesh_nodes[0][1]['mesh']!=0
            or type(mesh_nodes[0][1].get('skin')) is not int or mesh_nodes[0][1]['skin']!=0): raise ValueError('unsupported mesh association')
    mesh=meshes[0]; prim=mesh.get('primitives') if isinstance(mesh,dict) else None
    if not isinstance(prim,list) or len(prim)!=1 or not isinstance(prim[0],dict): raise ValueError('unsupported mesh')
    primitive=prim[0]; attrs=primitive.get('attributes')
    if (primitive.get('mode',4)!=4 or primitive.get('targets') or not isinstance(attrs,dict)
            or (set(attrs)!={'POSITION','JOINTS_0','WEIGHTS_0'} if not textured else
                not ({'POSITION','JOINTS_0','WEIGHTS_0','TEXCOORD_0'} <= set(attrs) <= {'POSITION','JOINTS_0','WEIGHTS_0','TEXCOORD_0','NORMAL'}))): raise ValueError('unsupported attributes')
    accessors=doc.get('accessors'); views=doc.get('bufferViews'); buffers=doc.get('buffers')
    if (not isinstance(accessors,list) or not isinstance(views,list) or not isinstance(buffers,list) or len(buffers)!=1
            or not isinstance(buffers[0],dict) or type(buffers[0].get('byteLength')) is not int or buffers[0]['byteLength']!=len(binary)):
        raise ValueError('unsupported rig buffers')
    def accessor(index):
        if type(index) is not int or not 0<=index<len(accessors) or not isinstance(accessors[index],dict): raise ValueError('invalid accessor')
        return accessors[index]
    def read_accessor(index, kind, component, width, fmt, max_count=100000):
        a=accessor(index)
        if not isinstance(a,dict) or a.get('type')!=kind or a.get('componentType')!=component or a.get('sparse') is not None or a.get('normalized',False) is not False: raise ValueError('unsupported accessor')
        count=a.get('count'); vi=a.get('bufferView')
        if type(count) is not int or not 1<=count<=max_count or type(vi) is not int or not 0<=vi<len(views): raise ValueError('invalid accessor')
        v=views[vi]
        if not isinstance(v,dict): raise ValueError('invalid accessor span')
        start=v.get('byteOffset',0); size=v.get('byteLength'); local=a.get('byteOffset',0); unit=struct.calcsize(fmt)
        if (type(v.get('buffer')) is not int or v['buffer']!=0 or v.get('byteStride') is not None or any(type(x) is not int or x<0 for x in (start,size,local)) or (start+local)%unit or local+count*unit*width>size or start+size>len(binary)): raise ValueError('invalid accessor span')
        return [item for item in struct.iter_unpack('<'+fmt*width,binary[start+local:start+local+count*unit*width])]
    position=read_accessor(attrs['POSITION'],'VEC3',5126,3,'f'); joint_accessor=accessor(attrs['JOINTS_0']); jc=joint_accessor.get('componentType')
    if jc not in (5121,5123): raise ValueError('unsupported joint encoding')
    encoded=read_accessor(attrs['JOINTS_0'],'VEC4',jc,4,'B' if jc==5121 else 'H'); weights=read_accessor(attrs['WEIGHTS_0'],'VEC4',5126,4,'f')
    if len(position)!=len(encoded) or len(position)!=len(weights): raise ValueError('vertex count mismatch')
    influenced=set()
    for pos,js,ws in zip(position,encoded,weights):
        if any(not math.isfinite(v) for v in pos+ws) or any(j>=len(joints) for j in js) or any(w<0 or w>1 for w in ws) or abs(sum(ws)-1)>0.001: raise ValueError('invalid rig weights')
        influenced.update(j for j,w in zip(js,ws) if w>0)
    if textured and 'inverseBindMatrices' not in skin: raise ValueError('bind matrices unavailable')
    if textured:
        uv=read_accessor(attrs['TEXCOORD_0'],'VEC2',5126,2,'f')
        if len(uv)!=len(position) or any(not math.isfinite(v) for row in uv for v in row): raise ValueError('invalid texture coordinates')
        if 'NORMAL' in attrs:
            normal=read_accessor(attrs['NORMAL'],'VEC3',5126,3,'f')
            if len(normal)!=len(position) or any(not math.isfinite(v) for row in normal for v in row): raise ValueError('invalid normals')
    if 'inverseBindMatrices' in skin:
        matrices=read_accessor(skin['inverseBindMatrices'],'MAT4',5126,16,'f',64)
        if len(matrices)!=len(joints) or any(not math.isfinite(v) for m in matrices for v in m): raise ValueError('invalid bind matrices')
    if 'indices' in primitive:
        ai=primitive['indices']; comp=accessor(ai).get('componentType')
        if comp not in (5121,5123,5125): raise ValueError('unsupported indices')
        indices=read_accessor(ai,'SCALAR',comp,1,{5121:'B',5123:'H',5125:'I'}[comp],1140000 if textured else 100000)
        if any(i[0]>=len(position) for i in indices): raise ValueError('invalid mesh indices')
    return {'sourceSHA256':digest,'skinIndex':0,'joints':[r for r in rows if r['jointOrdinal'] in influenced], 'hierarchy':rows}
