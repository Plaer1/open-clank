"""Private fixed local render wrapper. Only the J0 attachment authorizes it."""
import hashlib
import io
import json
import math
import os
import struct
from dataclasses import dataclass

from PIL import Image
from src import bg_jobs
from src.openclank.artifacts import media_artifact_owner

MAX_GLB = 64 * 1024 * 1024
MAX_PNG = 16 * 1024 * 1024

@dataclass(frozen=True)
class SettledRender:
    exit_code: int
    png: bytes | None
    importer: dict | None = None


def validate_glb(data, *, textured=False):
    if not isinstance(data, bytes) or not 20 <= len(data) <= MAX_GLB:
        raise ValueError('unsupported GLB size')
    magic, version, total = struct.unpack_from('<4sII', data)
    if magic != b'glTF' or version != 2 or total != len(data):
        raise ValueError('invalid GLB header')
    length, kind = struct.unpack_from('<I4s', data, 12)
    if kind != b'JSON' or length % 4 or length > 2 * 1024 * 1024 or 20 + length > len(data):
        raise ValueError('invalid GLB JSON chunk')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate GLB key')
            result[key] = value
        return result
    doc = json.loads(data[20:20+length].decode('utf-8'), object_pairs_hook=unique,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
    if not isinstance(doc, dict): raise ValueError('invalid GLB document')
    stack = [(doc, 0)]; visited = 0
    while stack:
        value, depth = stack.pop(); visited += 1
        if depth > 64 or visited > 200000: raise ValueError('GLB structure exceeds bounds')
        if isinstance(value, dict):
            if 'uri' in value: raise ValueError('GLB URI unsupported')
            if textured and 'extensions' in value: raise ValueError('GLB extensions unsupported')
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError('invalid GLB number')
    offset = 20 + length
    if offset < len(data):
        if offset + 8 > len(data): raise ValueError('truncated GLB chunk')
        binary_size, binary_kind = struct.unpack_from('<I4s', data, offset)
        if binary_kind != b'BIN\x00' or binary_size % 4 or offset + 8 + binary_size != len(data):
            raise ValueError('unsupported GLB chunks')
    if doc.get('extensionsUsed') or doc.get('extensionsRequired') or (doc.get('images') and not textured):
        raise ValueError('GLB extensions or images unsupported')
    if textured:
        _validate_embedded_textures(doc, data, offset)
    accessors = doc.get('accessors', [])
    if not isinstance(accessors, list) or len(accessors) > 10000:
        raise ValueError('GLB accessor budget exceeded')
    counts = [a.get('count') if isinstance(a, dict) else None for a in accessors]
    if any(type(count) is not int or not 0 <= count <= 1000000 for count in counts) or sum(counts) > 4000000:
        raise ValueError('GLB element budget exceeded')
    for field in ('buffers', 'images'):
        entries = doc.get(field, [])
        if not isinstance(entries, list) or len(entries) > 10000:
            raise ValueError('unsupported GLB resources')
        if any(not isinstance(item, dict) or 'uri' in item for item in entries):
            raise ValueError('external or embedded URI is unsupported')
    return hashlib.sha256(data).hexdigest()


def _validate_embedded_textures(doc, data, offset):
    """Narrow embedded PNG/JPEG subset; not a complete glTF validator."""
    import warnings
    from PIL import ImageFile
    if ImageFile.LOAD_TRUNCATED_IMAGES: raise ValueError('truncated image decoding unsupported')
    asset=doc.get('asset')
    if not isinstance(asset,dict) or asset.get('version')!='2.0': raise ValueError('unsupported GLB asset version')
    if offset + 8 > len(data): raise ValueError('GLB binary unavailable')
    binary_size, kind = struct.unpack_from('<I4s', data, offset)
    if kind != b'BIN\x00' or offset + 8 + binary_size != len(data): raise ValueError('invalid GLB binary')
    buffers, views, images = doc.get('buffers'), doc.get('bufferViews'), doc.get('images')
    if not isinstance(buffers,list) or len(buffers)!=1 or not isinstance(buffers[0],dict): raise ValueError('unsupported GLB buffer')
    declared=buffers[0].get('byteLength')
    if type(declared) is not int or declared<0 or declared>binary_size or binary_size-declared>3: raise ValueError('invalid GLB buffer size')
    if not isinstance(views,list) or len(views)>10000 or not isinstance(images,list) or not 1<=len(images)<=8: raise ValueError('unsupported GLB images')
    spans=[]
    for view in views:
        if not isinstance(view,dict) or type(view.get('buffer')) is not int or view['buffer']!=0: raise ValueError('unsupported GLB view')
        start=view.get('byteOffset',0); length=view.get('byteLength')
        if type(start) is not int or type(length) is not int or start<0 or length<=0 or start+length>declared: raise ValueError('invalid GLB view')
        spans.append((start,start+length))
    ordered=sorted(spans)
    if any(left[1]>right[0] for left,right in zip(ordered,ordered[1:])): raise ValueError('overlapping GLB views unsupported')
    pixels=total=0
    for image in images:
        if not isinstance(image,dict) or set(image)-{'bufferView','mimeType','name','extras'}: raise ValueError('unsupported GLB image')
        index=image.get('bufferView'); mime=image.get('mimeType')
        if type(index) is not int or not 0<=index<len(spans) or mime not in ('image/png','image/jpeg'): raise ValueError('invalid GLB image')
        start,end=spans[index]; length=end-start; total+=length
        if length>16*1024*1024 or total>32*1024*1024: raise ValueError('GLB image budget exceeded')
        raw=data[offset+8+start:offset+8+end]
        if not (raw.startswith(b'\x89PNG\r\n\x1a\n') if mime=='image/png' else raw.startswith(b'\xff\xd8\xff')): raise ValueError('GLB image MIME mismatch')
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as decoded:
                width,height=decoded.size
                if decoded.format!=('PNG' if mime=='image/png' else 'JPEG') or not 1<=width<=4096 or not 1<=height<=4096 or width*height>16777216 or getattr(decoded,'n_frames',1)!=1: raise ValueError('unsupported GLB image')
                pixels+=width*height
                if pixels>32000000: raise ValueError('GLB pixel budget exceeded')
                decoded.load()
    textures=doc.get('textures',[]); samplers=doc.get('samplers',[])
    if not isinstance(textures,list) or not textures or len(textures)>10000 or not isinstance(samplers,list) or len(samplers)>10000: raise ValueError('invalid GLB textures')
    for sampler in samplers:
        if not isinstance(sampler,dict) or set(sampler)-{'name','extras','magFilter','minFilter','wrapS','wrapT'}: raise ValueError('invalid GLB sampler')
        for field,allowed in (('magFilter',{9728,9729}),('minFilter',{9728,9729,9984,9985,9986,9987}),('wrapS',{33071,33648,10497}),('wrapT',{33071,33648,10497})):
            if field in sampler and (type(sampler[field]) is not int or sampler[field] not in allowed): raise ValueError('invalid GLB sampler')
    for texture in textures:
        if not isinstance(texture,dict) or type(texture.get('source')) is not int or not 0<=texture['source']<len(images): raise ValueError('invalid GLB texture')
        if 'sampler' in texture and (type(texture['sampler']) is not int or not 0<=texture['sampler']<len(samplers)): raise ValueError('invalid GLB sampler')
    material=doc.get('materials',[])
    if not isinstance(material,list) or len(material)>10000: raise ValueError('invalid GLB materials')
    used=0; stack=[material]
    while stack:
        value=stack.pop()
        if isinstance(value,list): stack.extend(value)
        elif isinstance(value,dict):
            for key,child in value.items():
                if key in ('baseColorTexture','metallicRoughnessTexture','normalTexture','occlusionTexture','emissiveTexture'):
                    if not isinstance(child,dict) or type(child.get('index')) is not int or not 0<=child['index']<len(textures): raise ValueError('invalid GLB material texture')
                    used+=1
                stack.append(child)
    if not used: raise ValueError('GLB texture not referenced')


def artifact_authority(store):
    """Bind custody to the injected store root and its actual SQL engine."""
    session = store._session_factory()
    try:
        bind = session.get_bind()
        database = bind.url.database
        if bind.url.get_backend_name() != 'sqlite' or not database or database == ':memory:':
            raise ValueError('artifact database identity unavailable')
        db_path = os.path.realpath(database)
        db_info = os.stat(db_path, follow_symlinks=False)
        root_info = os.stat(store.root, follow_symlinks=False)
        material = json.dumps([str(store.root.resolve()), root_info.st_dev, root_info.st_ino, db_path, db_info.st_dev, db_info.st_ino], separators=(',',':')).encode('utf-8')
        return hashlib.sha256(material).hexdigest()
    finally:
        session.close()


def assemble_four(root, *, rig=False):
    """Validate fixed private views and assemble one bounded PNG sheet."""
    import stat
    expected = {'input.glb','runner.py','view0.png','view1.png','view2.png','view3.png'}
    if rig: expected |= {'rig.json','importer.json'}
    if set(os.listdir(root)) != expected: raise ValueError('unexpected Blender output')
    sheet = Image.new('RGBA',(1024,1024))
    try:
        for index,position in enumerate(((0,0),(512,0),(0,512),(512,512))):
            fd = os.open(root / ('view%d.png' % index), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PNG: raise ValueError('unsupported Blender view')
                with os.fdopen(fd,'rb',closefd=False) as handle: raw = handle.read(MAX_PNG + 1)
                after = os.fstat(fd)
                if len(raw) != info.st_size or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                    raise ValueError('Blender view changed')
            finally:
                os.close(fd)
            with Image.open(io.BytesIO(raw)) as view:
                if view.format != 'PNG' or view.size != (512,512): raise ValueError('invalid Blender view')
                view.load()
                converted = view.convert('RGBA')
                try: sheet.paste(converted,position)
                finally: converted.close()
        output = io.BytesIO(); sheet.save(output,format='PNG'); data = output.getvalue()
        if not 0 < len(data) <= MAX_PNG: raise ValueError('Blender sheet exceeds bounds')
        return data
    finally:
        sheet.close()


def run(*, job_id, account_id, child_id, nonce, source, execute, artifact_store):
    """Claim once; only explicit settled child results can reach terminal."""
    child = bg_jobs.claim_media_local_child(job_id, account_id=account_id, child_id=child_id, nonce=nonce)
    if artifact_authority(artifact_store) != child['profile']['artifact_authority']:
        raise ValueError('artifact authority changed')
    if validate_glb(source, textured=child['profile']['adapter'] == 'blender-material-bake-v1') != child['profile']['input_sha256']:
        raise ValueError('input changed')
    if child['profile']['adapter'] == 'blender-material-bake-v1':
        from src.openclank.blender_material_inspect import inspect_material_source
        if inspect_material_source(source)['source_sha256'] != child['profile']['input_sha256']: raise ValueError('bake source changed')
    if child['profile']['adapter'] == 'blender-glb-rig-clip-v1':
        from src.openclank.blender_rig_inspect import inspect_rig
        inspected=inspect_rig(source); witness=child['profile']['rig']
        if (inspected['sourceSHA256']!=witness['sourceSHA256'] or inspected['hierarchy']!=witness['hierarchy']
                or witness['selected'] not in inspected['joints']): raise ValueError('rig source changed')
    outcome = execute(source, child['profile'])
    if not isinstance(outcome, SettledRender):
        raise RuntimeError('child settlement unknown')
    if outcome.exit_code != 0:
        receipt = {'exit_code':outcome.exit_code, 'output_sha256':None, 'output_size':0}
        return bg_jobs.finish_media_local_child(job_id, account_id=account_id, child_id=child_id, nonce=nonce, receipt=receipt)
    data = outcome.png
    bake = child['profile']['adapter'] == 'blender-material-bake-v1'
    if bake:
        from src.openclank.material_bundle import validate_bundle
        validate_bundle(data, expected_source_sha256=child['profile']['input_sha256'], expected_resolution=child['profile']['resolution'])
    elif not isinstance(data, bytes) or not 0 < len(data) <= MAX_PNG:
        raise ValueError('invalid render output')
    rigclip = child['profile']['adapter'] == 'blender-glb-rig-clip-v1'
    four = child['profile']['adapter'] == 'blender-glb-four-heading-v1' or rigclip
    if not bake:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.size != ((1024,1024) if four else (512,512)):
                raise ValueError('invalid render dimensions')
            image.verify()
    digest = hashlib.sha256(data).hexdigest()
    current = bg_jobs.get_media(job_id, account_id=account_id)
    if not current or current.get('local_child') != child:
        raise ValueError('local child authority changed')
    owner = media_artifact_owner(account_id)
    artifact = artifact_store.put(owner=owner, chunks=[data], media_type='application/zip' if bake else 'image/png')
    retained = b''.join(artifact_store.read_chunks(owner=owner, artifact_id=artifact.id))
    if len(retained) != len(data) or hashlib.sha256(retained).hexdigest() != digest:
        raise ValueError('retained artifact changed')
    if bake:
        validate_bundle(retained, expected_source_sha256=child['profile']['input_sha256'], expected_resolution=child['profile']['resolution'])
    artifact = artifact_store.acknowledge(owner=owner, artifact_id=artifact.id)
    if artifact.content_sha256 != digest or artifact.size_bytes != len(data):
        raise ValueError('artifact receipt mismatch')
    receipt = {'exit_code':0, 'output_sha256':digest, 'output_size':len(data), 'artifact_id':artifact.id}
    if rigclip:
        from src.openclank.blender_rig_metadata import layout_for
        receipt['layout'] = layout_for(digest,child['profile']['rig'],outcome.importer)
    elif four:
        from src.openclank.blender_four_layout import layout_for
        receipt['layout'] = layout_for(digest)
    return bg_jobs.finish_media_local_child(job_id, account_id=account_id, child_id=child_id, nonce=nonce, receipt=receipt)


def execute_fixed(source, profile, *, executable_path, runner_path, timeout=300):
    """Own and wait one configured Blender child; no caller-provided argv."""
    import os
    import shutil
    import signal
    import stat
    import subprocess
    import tempfile
    from pathlib import Path

    if os.name != 'posix' or not os.path.isdir('/proc/self/fd') or not 1 <= timeout <= 600:
        raise ValueError('local Blender execution unavailable')
    path = os.path.abspath(executable_path)
    if path != os.path.realpath(path):
        raise ValueError('Blender executable symlink unsupported')
    binary_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    owned = None
    waited = False
    process = None
    try:
        info = os.fstat(binary_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 * 1024:
            raise ValueError('unsupported Blender executable')
        with os.fdopen(os.dup(binary_fd), 'rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != profile['executable_sha256']:
            raise ValueError('Blender executable changed')
        runner = Path(runner_path).read_bytes()
        if len(runner) > 128 * 1024 or hashlib.sha256(runner).hexdigest() != profile['runner_sha256']:
            raise ValueError('Blender runner changed')
        owned = tempfile.mkdtemp(prefix='openclank-blender-')
        root = Path(owned)
        source_path, script_path, output_path = root/'input.glb', root/'runner.py', root/'output.png'
        for target, data in ((source_path, source), (script_path, runner)):
            with open(target, 'xb') as handle:
                handle.write(data)
            os.chmod(target, 0o400)
        bake = profile['adapter'] == 'blender-material-bake-v1'
        rigclip = profile['adapter'] == 'blender-glb-rig-clip-v1'
        four = profile['adapter'] == 'blender-glb-four-heading-v1' or rigclip
        if rigclip:
            rig_path=root/'rig.json'
            with open(rig_path,'xb') as handle: handle.write(json.dumps(profile['rig'],separators=(',',':')).encode('utf-8'))
            os.chmod(rig_path,0o400)
        process = subprocess.Popen([f'/proc/self/fd/{binary_fd}', '--background', '--factory-startup',
            '--disable-autoexec', '--python-exit-code', '1', '--python', str(script_path), '--',
            str(source_path), str(root if (four or bake) else output_path)] + ([str(profile['resolution'])] if bake else [str(rig_path)] if rigclip else []), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env={'PATH':os.defpath}, pass_fds=(binary_fd,), start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            # Process-group delivery does not prove all descendants exited.
            raise RuntimeError('local render descendant settlement unknown')
        waited = True
        if code != 0:
            return SettledRender(code, None)
        if bake:
            from src.openclank.material_bundle import pack_baked, validate_bundle, MAX_MAP
            names=('base-color.png','roughness.png','metallic.png')
            if {p.name for p in root.iterdir()} != {'input.glb','runner.py',*names}: raise ValueError('unexpected bake output')
            maps=[]
            for role,name in zip(('baseColor','roughness','metallic'),names):
                fd=os.open(root/name,os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_MAP: raise ValueError('invalid bake output')
                    with os.fdopen(fd,'rb',closefd=False) as handle: raw=handle.read(MAX_MAP+1)
                    if len(raw)!=info.st_size: raise ValueError('bake output changed')
                    maps.append((role,raw))
                finally: os.close(fd)
            data,_=pack_baked(maps,profile['input_sha256'],profile['resolution'])
            validate_bundle(data,expected_source_sha256=profile['input_sha256'],expected_resolution=profile['resolution'])
            return SettledRender(code,data)
        if four:
            data=assemble_four(root,rig=rigclip)
            if rigclip:
                fd=os.open(root/'importer.json',os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=4096: raise ValueError('importer provenance unavailable')
                    with os.fdopen(fd,'rb',closefd=False) as handle: raw=handle.read(4097)
                    if len(raw)!=info.st_size: raise ValueError('importer provenance changed')
                finally: os.close(fd)
                importer=json.loads(raw)
                return SettledRender(code,data,importer)
            return SettledRender(code,data)
        output_fd = os.open(output_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            output_info = os.fstat(output_fd)
            if not stat.S_ISREG(output_info.st_mode) or not 0 < output_info.st_size <= MAX_PNG:
                raise ValueError('unsupported Blender output')
            with os.fdopen(output_fd, 'rb', closefd=False) as handle:
                data = handle.read(MAX_PNG + 1)
            if len(data) != output_info.st_size:
                raise ValueError('Blender output changed')
            return SettledRender(code, data)
        finally:
            os.close(output_fd)
    finally:
        os.close(binary_fd)
        # Unknown process settlement retains its private working directory.
        if owned and (process is None or waited):
            shutil.rmtree(owned)


def _worker_main(child_id):
    import os
    import re
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.openclank.artifacts import ArtifactStore
    if not re.fullmatch(r'[0-9a-f]{32}', child_id): raise ValueError('invalid child')
    stage = bg_jobs._JOBS_DIR / ('blender-' + child_id)
    if stage.is_symlink() or not stage.is_dir(): raise ValueError('invalid child stage')
    def bounded(name, limit):
        fd = os.open(stage/name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, 'rb', closefd=False) as handle: data = handle.read(limit+1)
        finally: os.close(fd)
        if len(data) > limit: raise ValueError('child stage exceeds bounds')
        return data
    descriptor = json.loads(bounded('child.json', 16384))
    if not isinstance(descriptor, dict) or set(descriptor) != {'job_id','account_id','child_id','nonce','profile','executable_path','artifact_root','artifact_db'} or descriptor['child_id'] != child_id:
        raise ValueError('invalid child descriptor')
    source = bounded('input.glb', MAX_GLB)
    engine = create_engine('sqlite:///' + descriptor['artifact_db'], connect_args={'check_same_thread':False})
    try:
        store = ArtifactStore(Path(descriptor['artifact_root']), session_factory=sessionmaker(bind=engine))
        adapter = descriptor['profile'].get('adapter')
        if adapter not in {'blender-glb-png-v1','blender-glb-four-heading-v1','blender-glb-rig-clip-v1','blender-material-bake-v1'}: raise ValueError('unsupported Blender child')
        runner = Path(__file__).with_name({'blender-glb-rig-clip-v1':'blender_rig_runner.py','blender-glb-four-heading-v1':'blender_four_runner.py','blender-glb-png-v1':'blender_runner.py','blender-material-bake-v1':'blender_material_runner.py'}[adapter])
        run(job_id=descriptor['job_id'], account_id=descriptor['account_id'], child_id=child_id,
            nonce=descriptor['nonce'], source=source, artifact_store=store,
            execute=lambda data, profile:execute_fixed(data, profile, executable_path=descriptor['executable_path'], runner_path=runner))
    finally:
        engine.dispose()


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2: raise SystemExit(2)
    _worker_main(sys.argv[1])
