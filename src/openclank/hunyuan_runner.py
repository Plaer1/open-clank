"""Fixed offline Hunyuan image-to-untextured-GLB child entry."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
import sys
import stat

from src.openclank.hunyuan_recipe import DEVICES, FIXED_SHA256, FIXED_SIZES, REQUIRED_COMPONENTS
# The common custody helper is first-party code and is part of the original
# child profile's bound source, not a user-selected model component.
_HELPER_SHA256 = 'ff25768ee387b031910ab137d428d7cc3d6df32a894f054528c3792afe2c7b08'
import src.openclank as _package
if len(_package.__path__) != 1: raise ValueError('custody helper unavailable')
_helper_path = os.path.join(os.path.realpath(_package.__path__[0]), 'trellis_runner.py')
_fd = os.open(_helper_path, os.O_RDONLY | os.O_NOFOLLOW)
try:
    _before = os.fstat(_fd)
    if not stat.S_ISREG(_before.st_mode) or not 0 < _before.st_size <= 2 * 1024 * 1024:
        raise ValueError('custody helper unavailable')
    with os.fdopen(os.dup(_fd), 'rb') as _stream:
        _digest = hashlib.file_digest(_stream, 'sha256').hexdigest()
    _after = os.fstat(_fd)
    _identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if _digest != _HELPER_SHA256 or _identity(_before) != _identity(_after) or _identity(_before) != _identity(os.lstat(_helper_path)):
        raise ValueError('custody helper changed')
finally:
    os.close(_fd)
import src.openclank.trellis_runner as _custody
if os.path.realpath(_custody.__file__) != _helper_path: raise ValueError('custody helper changed')
from src.openclank.trellis_runner import _hash_file, _json, _regular, _weight


def main(descriptor_path):
    descriptor = _json(_regular(descriptor_path, 65536))
    if (type(descriptor) is not dict or set(descriptor) != {'version','seed','input','output','device','components'}
        or type(descriptor['version']) is not int or descriptor['version'] != 1
        or type(descriptor['seed']) is not int or not 0 <= descriptor['seed'] <= 2147483647
        or descriptor['device'] not in DEVICES or type(descriptor['components']) is not dict
        or set(descriptor['components']) != set(REQUIRED_COMPONENTS)):
        raise ValueError('recipe unavailable')
    items = descriptor['components']
    for role, item in items.items():
        if (type(item) is not dict or set(item) != {'path','size','sha256'}
            or type(item['path']) is not str or type(item['size']) is not int
            or item['size'] <= 0 or item['sha256'] != FIXED_SHA256[role]
            or (role in FIXED_SIZES and item['size'] != FIXED_SIZES[role])):
            raise ValueError('component unavailable')
        if role != 'weights' and (item['size'] > 2 * 1024 * 1024 or _hash_file(item['path'], 2 * 1024 * 1024) != FIXED_SHA256[role]):
            raise ValueError('component changed')
    source = _regular(descriptor['input'], 4 * 1024 * 1024)
    from PIL import Image
    with Image.open(io.BytesIO(source)) as image:
        if image.format != 'PNG' or image.mode != 'RGBA' or image.width < 1 or image.height < 1 or image.width * image.height > 16777216:
            raise ValueError('RGBA image required')
        image.load()
        alpha = image.getchannel('A')
        bounds = alpha.getbbox()
        if bounds is None or bounds[2] - bounds[0] < 2 or bounds[3] - bounds[1] < 2:
            raise ValueError('foreground unavailable')
        image = image.copy()
    output = descriptor['output']
    if type(output) is not str or not os.path.isabs(output) or os.path.lexists(output): raise ValueError('output unavailable')

    # The original fixed manifest supplies the installed source root; offline
    # flags precede all model imports. No pretrained loader is called here.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    sys.path.insert(0, str(Path(items['code_pipeline']['path']).parents[2]))
    import torch
    import yaml
    from safetensors.torch import load_file
    import hy3dgen.shapegen.pipelines as pipeline_module
    import hy3dgen.shapegen.utils as utils_module
    import hy3dgen.shapegen.models.conditioner as conditioner_module
    import hy3dgen.shapegen.preprocessors as preprocessor_module
    for module, role in ((pipeline_module,'code_pipeline'),(utils_module,'code_utils'),
                         (conditioner_module,'code_conditioner'),(preprocessor_module,'code_preprocessor')):
        if os.path.realpath(module.__file__) != items[role]['path']: raise ValueError('installed code changed')
    raw = _regular(items['config']['path'], 65536)
    if hashlib.sha256(raw).hexdigest() != FIXED_SHA256['config']: raise ValueError('config changed')
    cfg = yaml.safe_load(raw)
    instantiate = pipeline_module.instantiate_from_config
    model = instantiate(cfg['model'])
    vae = instantiate(cfg['vae'])
    conditioner = instantiate(cfg['conditioner'])
    with _weight(items['weights']) as fd:
        state = load_file(f'/proc/self/fd/{fd}', device='cpu')
        if any(not key.startswith(('model.','vae.','conditioner.')) for key in state): raise ValueError('checkpoint unavailable')
        for prefix, target in (('model.',model),('vae.',vae),('conditioner.',conditioner)):
            part = {key[len(prefix):]: value for key,value in state.items() if key.startswith(prefix)}
            if not part: raise ValueError('checkpoint incomplete')
            target.load_state_dict(part, strict=True)
        del state
    scheduler = instantiate(cfg['scheduler'])
    processor = instantiate(cfg['image_processor'])
    device, dtype = ('cpu',torch.float32) if descriptor['device'] == 'cpu-fp32' else ('cuda',torch.float16)
    pipeline = pipeline_module.Hunyuan3DDiTFlowMatchingPipeline(model=model,vae=vae,conditioner=conditioner,
        scheduler=scheduler,image_processor=processor,device=device,dtype=dtype)
    generator = torch.Generator(device='cpu').manual_seed(descriptor['seed'])
    meshes = pipeline(image=image,num_inference_steps=50,guidance_scale=7.5,octree_resolution=384,
                      generator=generator,output_type='trimesh',enable_pbar=False)
    if not isinstance(meshes,list) or len(meshes) != 1 or meshes[0] is None: raise ValueError('mesh unavailable')
    meshes[0].export(output, file_type='glb')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--': raise SystemExit(2)
    main(sys.argv[2])
